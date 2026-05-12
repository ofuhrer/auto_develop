from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from agentic_devloop.artifacts import cleanup_task_artifacts
from agentic_devloop.config import load_project_config
from fnmatch import fnmatch

from agentic_devloop.models import (
    Decision,
    OverlapFinding,
    ReleaseOverlapReport,
    ReleasePlan,
    ReviewDecision,
    TaskContract,
)
from agentic_devloop.orchestrator import ExecutorProtocol, TaskRunResult, run_task
from agentic_devloop.yaml_io import load_yaml_model


@dataclass(frozen=True)
class ReleaseRunResult:
    release_id: str
    run_id: str
    summary_path: Path
    log_path: Path
    task_results: list[TaskRunResult]
    decision: Decision


def make_release_run_id(release_id: str, now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{release_id}_release"


def run_release(
    *,
    project_id: str,
    release_id: str,
    contract_paths: list[Path] | None = None,
    config_dir: Path = Path("configs"),
    contracts_dir: Path = Path("contracts"),
    runs_dir: Path = Path("runs"),
    executor: ExecutorProtocol | None = None,
    verification_timeout_seconds: int = 600,
    allow_dirty: bool = False,
    commit_on_accept: bool = False,
    merge_on_accept: bool = False,
    push_on_accept: bool = False,
    stop_on_failure: bool = True,
    execution_mode: str = "sequential",
    debug_keep_artifacts: bool = False,
    now: datetime | None = None,
    progress: Callable[[str], None] | None = None,
) -> ReleaseRunResult:
    if execution_mode not in {"sequential", "parallel"}:
        raise ValueError(f"unsupported execution mode: {execution_mode}")
    config = load_project_config(project_id, config_dir, validate_repo=True)
    _ensure_no_existing_worktrees(config.worktree_root)
    run_id = make_release_run_id(release_id, now)
    release_root = runs_dir / run_id
    release_root.mkdir(parents=True, exist_ok=True)
    log_path = release_root / "release.log"
    progress = _multiplexed_progress(progress, log_path)
    selected_contracts = _select_contracts(
        release_id=release_id,
        config_repo_path=config.repo_path,
        repo_state_path=config.repo_state_path,
        contracts_dir=contracts_dir,
        contract_paths=contract_paths,
    )
    if not selected_contracts:
        raise ValueError(f"no contracts found for release {release_id}")
    selected_tasks = [load_yaml_model(path, TaskContract) for path in selected_contracts]
    overlap_report = analyze_contract_overlaps(selected_tasks)
    if overlap_report.has_blocking_findings or (
        execution_mode == "parallel" and overlap_report.has_parallel_blockers
    ):
        details = "; ".join(
            f"{finding.first_task_id}/{finding.second_task_id}: {finding.pattern}"
            for finding in overlap_report.findings
            if finding.severity in {"broad", "blocking"}
        )
        raise ValueError(f"release contracts are unsafe for {execution_mode} execution: {details}")

    _report(progress, f"release_run_id={run_id}")
    _report(progress, f"release_log={log_path}")
    _report(progress, f"release={release_id} tasks={len(selected_contracts)} mode={execution_mode}")
    if overlap_report.findings:
        _report(progress, f"overlap_findings={len(overlap_report.findings)} sequential_only=true")

    task_results: list[TaskRunResult] = []
    for index, (contract_path, task) in enumerate(zip(selected_contracts, selected_tasks), start=1):
        if task.release_id != release_id:
            raise ValueError(
                f"contract {contract_path} belongs to release {task.release_id}, expected {release_id}"
            )

        _report(progress, f"task {index}/{len(selected_contracts)} {task.task_id}")
        result = run_task(
            project_id=project_id,
            contract_path=contract_path,
            config_dir=config_dir,
            runs_dir=runs_dir,
            executor=executor,
            verification_timeout_seconds=verification_timeout_seconds,
            allow_dirty=allow_dirty,
            commit_on_accept=commit_on_accept,
            merge_on_accept=merge_on_accept,
            push_on_accept=push_on_accept,
            commit_message=f"{task.task_id}: {task.title}",
            progress=progress,
        )
        task_results.append(result)
        if debug_keep_artifacts:
            _report(progress, f"debug_keep_artifacts=true worktree={result.worktree_path}")
        else:
            for message in cleanup_task_artifacts(
                repo_path=config.repo_path,
                worktree_path=result.worktree_path,
                branch=f"agent/{task.release_id}/{task.task_id}",
                preserve_worktree=_should_preserve_task_worktree(result),
                preserve_branch=_should_preserve_task_branch(result),
            ):
                _report(progress, message)
        if stop_on_failure and result.decision.decision != Decision.ACCEPTED:
            _report(progress, f"stopping release after {task.task_id}: {result.decision.decision}")
            break

    decision = _release_decision([result.decision for result in task_results])
    summary_path = _write_release_summary(
        runs_dir=runs_dir,
        run_id=run_id,
        release_id=release_id,
        decision=decision,
        task_results=task_results,
        log_path=log_path,
    )
    _report(progress, f"release_decision={decision}")

    return ReleaseRunResult(
        release_id=release_id,
        run_id=run_id,
        summary_path=summary_path,
        log_path=log_path,
        task_results=task_results,
        decision=decision,
    )


def _select_contracts(
    *,
    release_id: str,
    config_repo_path: Path,
    repo_state_path: Path | None,
    contracts_dir: Path,
    contract_paths: list[Path] | None,
) -> list[Path]:
    if contract_paths:
        return contract_paths

    release_plan = _load_release_plan(config_repo_path, repo_state_path)
    if release_plan is not None and release_plan.release_id == release_id:
        return [contracts_dir / f"{task_id}.yaml" for task_id in release_plan.current_tasks]

    contracts = []
    for path in sorted(contracts_dir.glob("*.yaml")):
        task = load_yaml_model(path, TaskContract)
        if task.release_id == release_id:
            contracts.append(path)
    return contracts


def _load_release_plan(config_repo_path: Path, repo_state_path: Path | None) -> ReleasePlan | None:
    if repo_state_path is None:
        return None
    root = repo_state_path if repo_state_path.is_absolute() else config_repo_path / repo_state_path
    path = root / "release_plan.yaml"
    if not path.exists():
        return None
    return load_yaml_model(path, ReleasePlan)


def _ensure_no_existing_worktrees(worktree_root: Path) -> None:
    if not worktree_root.exists():
        return
    existing = sorted(
        path for path in worktree_root.iterdir() if path.is_dir() and not path.name.startswith(".")
    )
    if not existing:
        return
    listed = ", ".join(str(path) for path in existing[:5])
    suffix = "" if len(existing) <= 5 else f", ... (+{len(existing) - 5} more)"
    raise ValueError(
        "project worktree root is not clean before release start: "
        f"{listed}{suffix}. Inspect or remove stale worktrees before running run-release."
    )


def _release_decision(decisions: list[ReviewDecision]) -> Decision:
    if decisions and all(decision.decision == Decision.ACCEPTED for decision in decisions):
        return Decision.ACCEPTED
    if any(decision.decision == Decision.ESCALATED for decision in decisions):
        return Decision.ESCALATED
    return Decision.FAILED


def _write_release_summary(
    *,
    runs_dir: Path,
    run_id: str,
    release_id: str,
    decision: Decision,
    task_results: list[TaskRunResult],
    log_path: Path,
) -> Path:
    summary_dir = runs_dir / run_id
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summary_dir / "release_summary.json"
    summary = {
        "run_id": run_id,
        "release_id": release_id,
        "decision": decision,
        "log_path": str(log_path),
        "tasks": [
            {
                "task_id": result.decision.task_id,
                "run_id": result.run_id,
                "decision": result.decision.decision,
                "rationale": result.decision.rationale,
                "bundle_path": str(result.bundle_path),
                "commit_hash": result.finalize.commit_hash if result.finalize else None,
                "merged": result.finalize.merged if result.finalize else False,
                "pushed": result.finalize.pushed if result.finalize else False,
            }
            for result in task_results
        ],
    }
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary_path


def analyze_contract_overlaps(tasks: list[TaskContract]) -> ReleaseOverlapReport:
    findings: list[OverlapFinding] = []
    for index, first in enumerate(tasks):
        for second in tasks[index + 1 :]:
            for first_pattern in first.allowed_files:
                for second_pattern in second.allowed_files:
                    severity = _overlap_severity(first_pattern, second_pattern)
                    if severity is not None:
                        findings.append(
                            OverlapFinding(
                                first_task_id=first.task_id,
                                second_task_id=second.task_id,
                                pattern=f"{first_pattern} <-> {second_pattern}",
                                severity=severity,
                            )
                        )
    return ReleaseOverlapReport(findings=findings)


def _overlap_severity(first: str, second: str) -> str | None:
    if not _patterns_overlap(first, second):
        return None
    if _is_broad_pattern(first) or _is_broad_pattern(second):
        return "broad"
    if not _has_glob(first) and not _has_glob(second) and first == second:
        return "blocking"
    return "minor"


def _patterns_overlap(first: str, second: str) -> bool:
    if first == second:
        return True
    if first == "**" or second == "**":
        return True
    first_prefix = _glob_prefix(first)
    second_prefix = _glob_prefix(second)
    if first_prefix and second_prefix:
        return first_prefix.startswith(second_prefix) or second_prefix.startswith(first_prefix)
    return fnmatch(first, second) or fnmatch(second, first)


def _glob_prefix(pattern: str) -> str:
    wildcard_index = min([index for index in [pattern.find("*"), pattern.find("?")] if index >= 0], default=-1)
    if wildcard_index < 0:
        return pattern
    return pattern[:wildcard_index].rstrip("/")


def _has_glob(pattern: str) -> bool:
    return "*" in pattern or "?" in pattern


def _is_broad_pattern(pattern: str) -> bool:
    normalized = pattern.strip().rstrip("/")
    if normalized in {"*", "**", "**/*"}:
        return True
    if normalized.endswith("/**"):
        prefix = normalized.removesuffix("/**")
        return "/" not in prefix
    if normalized.endswith("/**/*"):
        prefix = normalized.removesuffix("/**/*")
        return "/" not in prefix
    return False


def _report(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _should_preserve_task_branch(result: TaskRunResult) -> bool:
    if result.finalize is None:
        return result.decision.decision == Decision.ACCEPTED
    if result.finalize.error is not None:
        return True
    return bool(result.finalize.commit_hash and not result.finalize.merged)


def _should_preserve_task_worktree(result: TaskRunResult) -> bool:
    if result.finalize is None:
        return result.decision.decision == Decision.ACCEPTED
    return result.finalize.error is not None


def _multiplexed_progress(
    progress: Callable[[str], None] | None,
    log_path: Path,
) -> Callable[[str], None]:
    lock = threading.Lock()

    def report(message: str) -> None:
        timestamp = datetime.now(UTC).isoformat()
        with lock:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as file:
                file.write(f"{timestamp} {message}\n")
        if progress is not None:
            progress(message)

    return report
