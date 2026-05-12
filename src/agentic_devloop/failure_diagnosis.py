from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Protocol, Sequence

from agentic_devloop.models import (
    CommandResult,
    ExecutorAttempt,
    ExecutorResult,
    FailureDiagnosis,
    FailureDiagnosisAttempt,
    FailureDiagnosisGuidance,
    FailureDiagnosisInput,
    FailureDiagnosisSourceMetadata,
    FailureEvidenceExcerpt,
    TaskContract,
)

DEFAULT_BACKEND_NAME = "deterministic_failure_diagnosis"
DEFAULT_PROMPT_CHAR_LIMIT = 12_000
DEFAULT_LOG_EXCERPT_CHAR_LIMIT = 2_000
DEFAULT_CHANGED_FILE_LIMIT = 20
DEFAULT_ATTEMPT_LIMIT = 10
DEFAULT_VERIFICATION_RESULT_LIMIT = 10


@dataclass(frozen=True)
class FailureDiagnosisRequest:
    task: TaskContract
    executor_result: ExecutorResult
    verification_results: Sequence[CommandResult]
    changed_files: Sequence[str]
    verification_log_path: Path | None = None
    backend_name: str = DEFAULT_BACKEND_NAME
    model: str | None = None
    max_prompt_chars: int = DEFAULT_PROMPT_CHAR_LIMIT
    log_excerpt_chars: int = DEFAULT_LOG_EXCERPT_CHAR_LIMIT


@dataclass(frozen=True)
class FailureDiagnosisBackendResult:
    prompt: str
    diagnosis: FailureDiagnosis


class FailureDiagnosisBackend(Protocol):
    def diagnose(self, request: FailureDiagnosisRequest) -> FailureDiagnosisBackendResult:
        ...


class DeterministicFailureDiagnosisBackend:
    def __init__(self, *, backend_name: str = DEFAULT_BACKEND_NAME, model: str | None = None) -> None:
        self.backend_name = backend_name
        self.model = model

    def diagnose(self, request: FailureDiagnosisRequest) -> FailureDiagnosisBackendResult:
        return FailureDiagnosisBackendResult(
            prompt=build_failure_diagnosis_prompt(request),
            diagnosis=_build_diagnosis(
                request,
                backend_name=self.backend_name,
                model=self.model if self.model is not None else request.model,
            ),
        )


def build_failure_diagnosis_prompt(request: FailureDiagnosisRequest) -> str:
    task = request.task
    result = request.executor_result
    attempts = _normalize_attempts(result.attempts, result)
    verification_results = list(request.verification_results)[:DEFAULT_VERIFICATION_RESULT_LIMIT]
    changed_files = list(request.changed_files[:DEFAULT_CHANGED_FILE_LIMIT])
    lines = [
        "# Failure Diagnosis Prompt",
        "",
        "Use only the evidence below. Classify the failure, explain the most likely cause, and recommend whether the task should be retried, narrowed, or escalated.",
        "",
        "## Contract Metadata",
        *_kv_lines(
            [
                ("task_id", task.task_id),
                ("release_id", task.release_id),
                ("title", task.title),
                ("task_type", str(task.task_type)),
                ("budget_class", task.budget_class),
                ("objective", task.objective),
                ("allowed_files", ", ".join(task.allowed_files)),
                ("required_evidence", ", ".join(task.required_evidence)),
                ("stop_conditions", ", ".join(task.stop_conditions)),
                ("non_goals", ", ".join(task.non_goals) if task.non_goals else "<none>"),
                (
                    "validation_assumptions",
                    ", ".join(task.validation_assumptions) if task.validation_assumptions else "<none>",
                ),
                ("fixture_changes_allowed", str(task.fixture_changes_allowed)),
                ("tolerance_changes_allowed", str(task.tolerance_changes_allowed)),
                ("benchmark_delta_required", str(task.benchmark_delta_required)),
                ("depends_on", ", ".join(task.depends_on) if task.depends_on else "<none>"),
                ("verification_profile", task.verification.profile or "<inline>"),
                ("verification_commands", ", ".join(task.verification.commands) if task.verification.commands else "<none>"),
            ]
        ),
        "",
        "## Executor Result",
        *_kv_lines(
            [
                ("backend", result.backend),
                ("model", result.model or "<none>"),
                ("command", _command_to_str(result.command)),
                ("exit_code", str(result.exit_code)),
                ("timed_out", str(result.timed_out)),
                ("duration_seconds", str(result.duration_seconds)),
                ("prompt_chars", str(result.prompt_chars)),
                ("stdout_chars", str(result.stdout_chars)),
                ("stderr_chars", str(result.stderr_chars)),
            ]
        ),
        "",
        "### Executor Attempts",
        *(_attempt_line(attempt) for attempt in attempts[:DEFAULT_ATTEMPT_LIMIT]),
        *([f"- ... {len(attempts) - DEFAULT_ATTEMPT_LIMIT} more attempt(s) omitted ..."] if len(attempts) > DEFAULT_ATTEMPT_LIMIT else []),
        "",
        "## Verification Results",
        *([_verification_line(item) for item in verification_results] or ["- <no verification results recorded>"]),
        *([f"- ... {len(request.verification_results) - DEFAULT_VERIFICATION_RESULT_LIMIT} more verification result(s) omitted ..."] if len(request.verification_results) > DEFAULT_VERIFICATION_RESULT_LIMIT else []),
        "",
        "## Changed Files",
        *([f"- {path}" for path in changed_files] or ["- <no changed files recorded>"]),
        *([f"- ... {len(request.changed_files) - len(changed_files)} more changed file(s) omitted ..."] if len(request.changed_files) > len(changed_files) else []),
        "",
        "## Logs",
        f"### {result.stderr_path.name}",
        _render_log_excerpt(result.stderr_path, _read_text(result.stderr_path), request.log_excerpt_chars),
        "",
        f"### {result.stdout_path.name}",
        _render_log_excerpt(result.stdout_path, _read_text(result.stdout_path), request.log_excerpt_chars),
    ]
    if request.verification_log_path is not None:
        lines += ["", f"### {request.verification_log_path.name}", _render_log_excerpt(request.verification_log_path, _read_text(request.verification_log_path), request.log_excerpt_chars)]
    lines += [
        "",
        "## Expected Response",
        "- category",
        "- confidence between 0 and 1",
        "- diagnosis_inputs",
        "- supporting_evidence_excerpts",
        "- recommendation",
        "- guidance.retryable",
        "- guidance.escalate",
    ]
    return _truncate_text("\n".join(lines) + "\n", request.max_prompt_chars)


def _build_diagnosis(request: FailureDiagnosisRequest, *, backend_name: str, model: str | None) -> FailureDiagnosis:
    result = request.executor_result
    attempts = _normalize_attempts(result.attempts, result)
    verification_exit_codes = [item.exit_code for item in request.verification_results]
    combined_logs = "\n".join(
        filter(None, (_read_text(result.stderr_path), _read_text(result.stdout_path), _read_text(request.verification_log_path) if request.verification_log_path else ""))
    ).lower()
    changed_file_violations = [path for path in request.changed_files if not any(fnmatch(path, pattern) for pattern in request.task.allowed_files)]
    category, recommendation, retryable, escalate, reason = _classify_failure(
        executor_result=result,
        verification_exit_codes=verification_exit_codes,
        changed_file_violations=changed_file_violations,
        combined_logs=combined_logs,
        attempt_count=len(attempts),
    )
    diagnosis_inputs = [
        FailureDiagnosisInput(name=name, value=value, source=source)
        for name, value, source in [
            ("task_id", request.task.task_id, "contract"),
            ("release_id", request.task.release_id, "contract"),
            ("task_type", str(request.task.task_type), "contract"),
            ("allowed_files", ", ".join(request.task.allowed_files), "contract"),
            ("executor_backend", result.backend, "executor_result"),
            ("executor_model", result.model or "<none>", "executor_result"),
            ("executor_exit_code", str(result.exit_code), "executor_result"),
            ("executor_timed_out", str(result.timed_out).lower(), "executor_result"),
            ("verification_exit_codes", ", ".join(str(code) for code in verification_exit_codes) if verification_exit_codes else "<none>", "verification_results"),
            ("changed_files", ", ".join(request.changed_files) if request.changed_files else "<none>", "workspace"),
            ("executor_attempt_count", str(len(attempts)), "executor_attempts"),
        ]
    ]
    excerpts = [
        FailureEvidenceExcerpt(source=result.stderr_path.name, excerpt=_excerpt(_read_text(result.stderr_path), request.log_excerpt_chars), path=result.stderr_path),
        FailureEvidenceExcerpt(source=result.stdout_path.name, excerpt=_excerpt(_read_text(result.stdout_path), request.log_excerpt_chars), path=result.stdout_path),
    ]
    if request.verification_log_path is not None:
        excerpts.append(
            FailureEvidenceExcerpt(
                source=request.verification_log_path.name,
                excerpt=_excerpt(_read_text(request.verification_log_path), request.log_excerpt_chars),
                path=request.verification_log_path,
            )
        )
    return FailureDiagnosis(
        diagnosis_inputs=diagnosis_inputs,
        category=category,
        confidence=0.91 if category != "executor_error" else 0.78,
        supporting_evidence_excerpts=excerpts,
        recommendation=recommendation,
        guidance=FailureDiagnosisGuidance(
            retryable=retryable,
            escalate=escalate,
            retry_reason=reason if retryable else None,
            escalate_reason=reason if escalate else None,
        ),
        source_metadata=FailureDiagnosisSourceMetadata(
            backend=backend_name,
            model=model,
            command=["deterministic-failure-diagnosis"],
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            stdout_path=result.stdout_path,
            stderr_path=result.stderr_path,
            attempts=[
                FailureDiagnosisAttempt(attempt=attempt.attempt, model=attempt.model, exit_code=attempt.exit_code, timed_out=attempt.timed_out)
                for attempt in attempts
            ],
        ),
    )


def _classify_failure(*, executor_result: ExecutorResult, verification_exit_codes: list[int], changed_file_violations: list[str], combined_logs: str, attempt_count: int) -> tuple[str, str, bool, bool, str]:
    if changed_file_violations:
        return ("contract_mismatch", "Narrow the task contract or move the changes back into the allowed file set before retrying.", False, True, "The task changed files outside the allowed scope.")
    if any(exit_code != 0 for exit_code in verification_exit_codes):
        return ("verification_failure", "Inspect the failing verification command(s), fix the underlying issue, and rerun the task.", True, attempt_count > 1, "Verification failed after execution completed.")
    if executor_result.timed_out or "timed out" in combined_logs or "timeout" in combined_logs:
        return ("timeout", "Reduce the task scope or increase walltime before retrying.", True, attempt_count > 1, "The executor or one of the captured logs indicates a timeout.")
    if "usage limit" in combined_logs or "quota" in combined_logs:
        return ("model_quota", "Retry with a fallback model or after the quota resets.", True, False, "The captured logs indicate a quota or usage-limit failure.")
    return ("executor_error", "Inspect the executor logs and retry only after the failure mode is understood.", True, attempt_count > 1, "No narrower category matched the captured evidence.")


def _normalize_attempts(attempts: Sequence[ExecutorAttempt], result: ExecutorResult) -> list[ExecutorAttempt]:
    return list(attempts) or [ExecutorAttempt(attempt=1, backend=result.backend, model=result.model, command=result.command, exit_code=result.exit_code, stdout_path=result.stdout_path, stderr_path=result.stderr_path, duration_seconds=result.duration_seconds, timed_out=result.timed_out, prompt_chars=result.prompt_chars, stdout_chars=result.stdout_chars, stderr_chars=result.stderr_chars)]


def _kv_lines(items: Sequence[tuple[str, str]]) -> list[str]:
    return [f"- {key}: {value}" for key, value in items]


def _attempt_line(attempt: ExecutorAttempt) -> str:
    return (
        f"- attempt {attempt.attempt}: backend={attempt.backend} model={attempt.model or '<none>'} "
        f"exit_code={attempt.exit_code} timed_out={attempt.timed_out} duration_seconds={attempt.duration_seconds} "
        f"command={_command_to_str(attempt.command)} prompt_chars={attempt.prompt_chars} "
        f"stdout_chars={attempt.stdout_chars} stderr_chars={attempt.stderr_chars}"
    )


def _verification_line(result: CommandResult) -> str:
    return f"- command={result.command} exit_code={result.exit_code} timed_out={result.timed_out} duration_seconds={result.duration_seconds}"


def _command_to_str(command: Sequence[str]) -> str:
    return " ".join(command)


def _read_text(path: Path | None) -> str:
    return "<missing log file>" if path is None or not path.exists() else path.read_text(encoding="utf-8")


def _render_log_excerpt(path: Path, text: str, max_chars: int) -> str:
    return "\n".join([f"- path: {path}", "```text", _excerpt(text, max_chars), "```"])


def _excerpt(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text.rstrip() or "<empty>"
    marker = "\n... <truncated> ..."
    return text[: max(0, max_chars - len(marker))].rstrip() + marker


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    marker = "\n\n... <prompt truncated> ...\n"
    return text[: max(0, max_chars - len(marker))].rstrip() + marker
