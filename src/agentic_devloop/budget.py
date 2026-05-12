from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any, Mapping

from agentic_devloop.models import Budget
from agentic_devloop.models import (
    BudgetLedger,
    BudgetFinding,
    BudgetTaskSummary,
    BudgetTuningReport,
    BudgetUsageEntry,
    ModelAttemptSummary,
)


@dataclass(frozen=True)
class BudgetLedgerEntry:
    release_id: str
    kind: str
    model: str
    reason: str
    created_at: str


def reserve_strong_model_call(
    *,
    runs_dir: Path,
    release_id: str,
    budget: Budget,
    model: str,
    reason: str,
    now: datetime | None = None,
) -> Path:
    ledger_path = runs_dir / release_id / "budget_ledger.json"
    entries = _read_entries(ledger_path)
    used = sum(1 for entry in entries if entry.get("kind") == "strong_model")
    if used >= budget.max_strong_model_calls_per_release:
        raise ValueError(
            "strong-model call budget exceeded: "
            f"{used}/{budget.max_strong_model_calls_per_release} already used for {release_id}"
        )

    created_at = (now or datetime.now(UTC)).isoformat()
    entry = BudgetLedgerEntry(
        release_id=release_id,
        kind="strong_model",
        model=model,
        reason=reason,
        created_at=created_at,
    )
    entries.append(entry.__dict__)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    return ledger_path


def _read_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def build_budget_ledger(*, release_metrics: Mapping[str, Any], budget: Budget) -> BudgetLedger:
    release_id = str(release_metrics.get("release_id", "<unknown>"))
    tasks = [_task_summary(task) for task in _as_sequence(release_metrics.get("tasks"))]
    model_attempts = _model_attempt_summaries(release_metrics)
    usage = _budget_usage_entries(budget, tasks, release_metrics)
    task_size_outliers = _task_size_outliers(budget, tasks)
    verification_bottlenecks = _verification_bottlenecks(tasks)
    waste_signals = _waste_signals(release_metrics)

    return BudgetLedger(
        release_id=release_id,
        budget=budget,
        usage=usage,
        task_summaries=tasks,
        model_attempts=model_attempts,
        task_size_outliers=task_size_outliers,
        verification_bottlenecks=verification_bottlenecks,
        waste_signals=waste_signals,
    )


def build_tuning_report(*, ledger: BudgetLedger) -> BudgetTuningReport:
    signals: list[str] = []
    recommendations: list[str] = []

    for entry in ledger.usage:
        if entry.utilization is None:
            continue
        if entry.utilization > 1:
            signals.append(
                f"{entry.name} exceeded budget: actual {entry.actual} {entry.unit} over configured {entry.configured}"
            )
            recommendations.append(f"Reduce {entry.name} to stay within the configured {entry.unit} budget.")
        elif entry.utilization == 1:
            signals.append(
                f"{entry.name} reached the configured budget ({entry.actual}/{entry.configured} {entry.unit})"
            )
            recommendations.append(f"Keep {entry.name} below the configured {entry.unit} budget on the next run.")
        elif entry.utilization >= 0.8:
            signals.append(
                f"{entry.name} is at {entry.utilization:.0%} of configured budget ({entry.actual}/{entry.configured} {entry.unit})"
            )
            recommendations.append(f"Trim {entry.name} before the task reaches the configured limit.")

    for outlier in ledger.task_size_outliers:
        signals.append(
            f"task {outlier.task_id} is a size outlier on {outlier.metric}: {outlier.actual}"
            + (
                f" against limit {outlier.configured_limit}"
                if outlier.configured_limit is not None
                else ""
            )
        )
        recommendations.append(
            f"Split or narrow task {outlier.task_id} to reduce {outlier.metric} pressure."
        )

    for bottleneck in ledger.verification_bottlenecks:
        signals.append(
            f"task {bottleneck.task_id} spent {bottleneck.verification_duration_seconds:.3f}s in verification across "
            f"{bottleneck.verification_command_count} commands"
        )
        recommendations.append(
            f"Shorten or scope down verification for task {bottleneck.task_id} if the command set is redundant."
        )

    for signal in ledger.waste_signals:
        signals.append(signal.message)
        recommendations.append(
            f"Review routing for task {signal.task_id}; the fallback model only masked a failed primary attempt."
        )

    if not signals:
        signals.append("No budget pressure signals were detected from the recorded release metrics.")
    if not recommendations:
        recommendations.append("No tuning changes are required from the observed metrics.")

    headline = f"Budget tuning guidance for {ledger.release_id}"
    return BudgetTuningReport(
        release_id=ledger.release_id,
        headline=headline,
        signals=signals,
        recommendations=list(dict.fromkeys(recommendations)),
    )


def _task_summary(task: Mapping[str, Any]) -> BudgetTaskSummary:
    attempts = _as_sequence(task.get("executor_attempts"))
    return BudgetTaskSummary(
        task_id=str(task.get("task_id", "<unknown>")),
        bundle_path=Path(task["bundle_path"]) if task.get("bundle_path") else None,
        decision=str(task.get("decision", "<unknown>")),
        changed_files=_int(task.get("changed_file_count")),
        diff_lines=_int(task.get("diff_lines")),
        context_chars=_int(task.get("context_chars")),
        prompt_chars=_int(task.get("prompt_chars")),
        output_chars=_int(task.get("stdout_chars")) + _int(task.get("stderr_chars")),
        verification_command_count=_int(task.get("verification_command_count")),
        verification_duration_seconds=_float(task.get("verification_duration_seconds")),
        executor_attempts=len(attempts),
    )


def _budget_usage_entries(
    budget: Budget,
    tasks: list[BudgetTaskSummary],
    release_metrics: Mapping[str, Any],
) -> list[BudgetUsageEntry]:
    max_changed_files = max((task.changed_files for task in tasks), default=0)
    max_diff_lines = max((task.diff_lines for task in tasks), default=0)
    max_context_chars = max((task.context_chars for task in tasks), default=0)
    max_prompt_chars = max((task.prompt_chars for task in tasks), default=0)
    max_output_chars = max((task.output_chars for task in tasks), default=0)
    max_executor_attempts = max((task.executor_attempts for task in tasks), default=0)
    total_executor_attempts = sum(task.executor_attempts for task in tasks)

    entries = [
        _usage_entry(
            name="executor_attempts_per_task",
            scope="task",
            unit="attempts",
            configured=budget.max_executor_attempts_per_task,
            actual=max_executor_attempts,
        ),
        _usage_entry(
            name="changed_files_per_task",
            scope="task",
            unit="files",
            configured=budget.max_changed_files_per_task,
            actual=max_changed_files,
        ),
        _usage_entry(
            name="diff_lines_per_task",
            scope="task",
            unit="lines",
            configured=budget.max_diff_lines_per_task,
            actual=max_diff_lines,
        ),
        _usage_entry(
            name="context_chars_per_task",
            scope="task",
            unit="chars",
            configured=budget.max_context_chars_per_task,
            actual=max_context_chars,
        ),
        _usage_entry(
            name="prompt_chars_per_task",
            scope="task",
            unit="chars",
            configured=None,
            actual=max_prompt_chars,
        ),
        _usage_entry(
            name="output_chars_per_task",
            scope="task",
            unit="chars",
            configured=None,
            actual=max_output_chars,
        ),
        _usage_entry(
            name="executor_attempts_total",
            scope="release",
            unit="attempts",
            configured=None,
            actual=total_executor_attempts,
        ),
    ]

    strong_model_calls = release_metrics.get("strong_model_calls")
    if strong_model_calls is not None:
        entries.append(
            _usage_entry(
                name="strong_model_calls_per_release",
                scope="release",
                unit="calls",
                configured=budget.max_strong_model_calls_per_release,
                actual=_int(strong_model_calls),
            )
        )

    return entries


def _model_attempt_summaries(release_metrics: Mapping[str, Any]) -> list[ModelAttemptSummary]:
    attempts_by_model: dict[str, dict[str, float | int]] = {}
    model_attempts = release_metrics.get("model_attempts")
    if isinstance(model_attempts, Mapping) and model_attempts:
        for model, payload in model_attempts.items():
            if not isinstance(payload, Mapping):
                continue
            attempts_by_model[str(model)] = {
                "attempts": _int(payload.get("attempts")),
                "successful_attempts": _int(payload.get("successful_attempts")),
                "failed_attempts": _int(payload.get("failed_attempts")),
                "duration_seconds": _float(payload.get("duration_seconds")),
                "prompt_chars": _int(payload.get("prompt_chars")),
                "stdout_chars": _int(payload.get("stdout_chars")),
                "stderr_chars": _int(payload.get("stderr_chars")),
            }
    else:
        for task in _as_sequence(release_metrics.get("tasks")):
            for attempt in _as_sequence(task.get("executor_attempts")):
                model = str(attempt.get("model") or "<none>")
                entry = attempts_by_model.setdefault(
                    model,
                    {
                        "attempts": 0,
                        "successful_attempts": 0,
                        "failed_attempts": 0,
                        "duration_seconds": 0.0,
                        "prompt_chars": 0,
                        "stdout_chars": 0,
                        "stderr_chars": 0,
                    },
                )
                entry["attempts"] = int(entry["attempts"]) + 1
                if _int(attempt.get("exit_code"), default=1) == 0:
                    entry["successful_attempts"] = int(entry["successful_attempts"]) + 1
                else:
                    entry["failed_attempts"] = int(entry["failed_attempts"]) + 1
                entry["duration_seconds"] = float(entry["duration_seconds"]) + _float(attempt.get("duration_seconds"))
                entry["prompt_chars"] = int(entry["prompt_chars"]) + _int(attempt.get("prompt_chars"))
                entry["stdout_chars"] = int(entry["stdout_chars"]) + _int(attempt.get("stdout_chars"))
                entry["stderr_chars"] = int(entry["stderr_chars"]) + _int(attempt.get("stderr_chars"))

    return [
        ModelAttemptSummary(
            model=model,
            attempts=int(payload["attempts"]),
            successful_attempts=int(payload["successful_attempts"]),
            failed_attempts=int(payload["failed_attempts"]),
            duration_seconds=round(float(payload["duration_seconds"]), 3),
            prompt_chars=int(payload["prompt_chars"]),
            stdout_chars=int(payload["stdout_chars"]),
            stderr_chars=int(payload["stderr_chars"]),
        )
        for model, payload in sorted(attempts_by_model.items())
    ]


def _task_size_outliers(budget: Budget, tasks: list[BudgetTaskSummary]) -> list[BudgetFinding]:
    findings: list[BudgetFinding] = []
    for metric, configured, unit, values in [
        ("changed_files", budget.max_changed_files_per_task, "files", [task.changed_files for task in tasks]),
        ("diff_lines", budget.max_diff_lines_per_task, "lines", [task.diff_lines for task in tasks]),
        ("context_chars", budget.max_context_chars_per_task, "chars", [task.context_chars for task in tasks]),
        ("prompt_chars", None, "chars", [task.prompt_chars for task in tasks]),
        ("output_chars", None, "chars", [task.output_chars for task in tasks]),
    ]:
        if not values:
            continue
        if configured is None:
            threshold = _outlier_threshold(values)
            findings.extend(
                BudgetFinding(kind="size_outlier", task_id=task.task_id, metric=metric, actual=value, message=f"{metric} is the largest observed task size")
                for task, value in zip(tasks, values, strict=False)
                if value > 0 and value >= threshold
            )
            continue
        findings.extend(
            BudgetFinding(
                kind="size_outlier",
                task_id=task.task_id,
                metric=metric,
                actual=value,
                configured_limit=configured,
                share_of_limit=round(value / configured, 3),
                message=f"{metric} reached {value}/{configured} {unit}",
            )
            for task, value in zip(tasks, values, strict=False)
            if value >= configured * 0.8
        )
    return findings


def _verification_bottlenecks(tasks: list[BudgetTaskSummary]) -> list[BudgetFinding]:
    if not tasks:
        return []
    threshold = max(sum(task.verification_duration_seconds for task in tasks) * 0.25, median([task.verification_duration_seconds for task in tasks]))
    return [
        BudgetFinding(
            kind="verification_bottleneck",
            task_id=task.task_id,
            verification_duration_seconds=round(task.verification_duration_seconds, 3),
            verification_command_count=task.verification_command_count,
            message=f"verification consumed {task.verification_duration_seconds:.3f}s across {task.verification_command_count} commands",
        )
        for task in sorted(tasks, key=lambda item: item.verification_duration_seconds, reverse=True)
        if task.verification_duration_seconds >= threshold > 0
    ]


def _waste_signals(release_metrics: Mapping[str, Any]) -> list[BudgetFinding]:
    signals: list[BudgetFinding] = []
    for task in _as_sequence(release_metrics.get("tasks")):
        attempts = _as_sequence(task.get("executor_attempts"))
        if len(attempts) < 2:
            continue
        primary = attempts[0]
        primary_model = str(primary.get("model") or "<none>")
        if _int(primary.get("exit_code"), default=1) == 0:
            continue
        for attempt in attempts[1:]:
            if _int(attempt.get("exit_code"), default=1) != 0:
                continue
            fallback_model = str(attempt.get("model") or "<none>")
            if fallback_model == primary_model:
                continue
            signals.append(
                BudgetFinding(
                    kind="waste_signal",
                    task_id=str(task.get("task_id", "<unknown>")),
                    message=(
                        f"task {task.get('task_id', '<unknown>')} failed on primary model {primary_model} "
                        f"before succeeding on fallback model {fallback_model}"
                    ),
                    primary_model=primary_model,
                    fallback_model=fallback_model,
                )
            )
            break
    return signals


def _usage_entry(
    *,
    name: str,
    scope: str,
    unit: str,
    configured: int | float | None,
    actual: int | float,
) -> BudgetUsageEntry:
    utilization: float | None = None
    remaining: int | float | None = None
    over_by: int | float | None = None
    if configured is not None and configured > 0:
        utilization = round(float(actual) / float(configured), 3)
        if actual <= configured:
            remaining = configured - actual
        else:
            over_by = actual - configured
    return BudgetUsageEntry(
        name=name,
        scope=scope,
        unit=unit,
        configured=configured,
        actual=actual,
        utilization=utilization,
        remaining=remaining,
        over_by=over_by,
    )


def _outlier_threshold(values: list[int | float]) -> int | float:
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    top = ordered[-1]
    next_highest = ordered[-2]
    return top if next_highest <= 0 or top >= next_highest * 1.2 else (top if top > 0 else 0)


def _as_sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
