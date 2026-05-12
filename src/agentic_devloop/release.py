from __future__ import annotations

import json
import shlex
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from agentic_devloop.artifacts import cleanup_task_artifacts
from agentic_devloop.config import load_project_config
from agentic_devloop.git_finalize import (
    FinalizeResult,
    GitFinalizeError,
    ensure_branch_from_base,
    merge_integration_branch_to_base,
    push_branch,
)
from fnmatch import fnmatch

from agentic_devloop.models import (
    Decision,
    OverlapFinding,
    ReleaseOverlapReport,
    ReleasePlan,
    ReviewDecision,
    TaskContract,
)
from agentic_devloop.orchestrator import ExecutorProtocol, TaskRunResult, branch_name, run_task
from agentic_devloop.process import run_process
from agentic_devloop.yaml_io import load_yaml_model


@dataclass(frozen=True)
class ReleaseRunResult:
    release_id: str
    run_id: str
    summary_path: Path
    log_path: Path
    review_path: Path
    metrics_path: Path
    task_results: list[TaskRunResult]
    decision: Decision
    integration_branch: str | None = None
    finalization: FinalizeResult | None = None


def make_release_run_id(release_id: str, now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{release_id}_release"


def feature_branch_name(release_id: str) -> str:
    return f"feature/{release_id}"


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
    release_finalize: str = "none",
    integration_branch: str | None = None,
    stop_on_failure: bool = True,
    execution_mode: str = "sequential",
    debug_keep_artifacts: bool = False,
    now: datetime | None = None,
    progress: Callable[[str], None] | None = None,
) -> ReleaseRunResult:
    if execution_mode not in {"sequential", "parallel"}:
        raise ValueError(f"unsupported execution mode: {execution_mode}")
    if release_finalize not in {"none", "merge-main", "push-feature", "push-main"}:
        raise ValueError(f"unsupported release finalization mode: {release_finalize}")
    config = load_project_config(project_id, config_dir, validate_repo=True)
    _ensure_no_existing_worktrees(config.worktree_root)
    run_id = make_release_run_id(release_id, now)
    release_root = runs_dir / run_id
    release_root.mkdir(parents=True, exist_ok=True)
    log_path = release_root / "release.log"
    raw_log_path = release_root / "release.raw.log"
    progress = _multiplexed_progress(progress, log_path, raw_log_path)
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
    for contract_path, task in zip(selected_contracts, selected_tasks):
        if task.release_id != release_id:
            raise ValueError(
                f"contract {contract_path} belongs to release {task.release_id}, expected {release_id}"
            )
    _ensure_no_existing_task_branches(config.repo_path, release_id, selected_tasks)
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

    _report(progress, f"event=release_started run_id={run_id} release={release_id} tasks={len(selected_contracts)} mode={execution_mode}")
    _report(progress, f"event=release_logs log={log_path} raw_log={raw_log_path}")
    feature_branch = integration_branch or feature_branch_name(release_id)
    ensure_branch_from_base(config.repo_path, feature_branch, config.default_base_branch)
    _report(progress, f"event=integration_branch branch={feature_branch} base={config.default_base_branch}")
    if overlap_report.findings:
        _report(progress, f"event=overlap_findings count={len(overlap_report.findings)}")

    task_inputs = list(zip(selected_contracts, selected_tasks))
    dependencies = _release_dependency_map(selected_tasks, overlap_report)
    if dependencies:
        _report(progress, "event=execution_dag dependencies=" + json.dumps(dependencies, sort_keys=True))
    if execution_mode == "parallel":
        task_results = _run_release_parallel(
            project_id=project_id,
            config_repo_path=config.repo_path,
            config_dir=config_dir,
            runs_dir=runs_dir,
            task_base_branch=feature_branch,
            task_inputs=task_inputs,
            dependencies=dependencies,
            executor=executor,
            verification_timeout_seconds=verification_timeout_seconds,
            allow_dirty=allow_dirty,
            commit_on_accept=commit_on_accept,
            merge_on_accept=merge_on_accept,
            push_on_accept=push_on_accept,
            stop_on_failure=stop_on_failure,
            debug_keep_artifacts=debug_keep_artifacts,
            progress=progress,
        )
    else:
        task_results = _run_release_sequential(
            project_id=project_id,
            config_repo_path=config.repo_path,
            config_dir=config_dir,
            runs_dir=runs_dir,
            task_base_branch=feature_branch,
            task_inputs=task_inputs,
            executor=executor,
            verification_timeout_seconds=verification_timeout_seconds,
            allow_dirty=allow_dirty,
            commit_on_accept=commit_on_accept,
            merge_on_accept=merge_on_accept,
            push_on_accept=push_on_accept,
            stop_on_failure=stop_on_failure,
            debug_keep_artifacts=debug_keep_artifacts,
            progress=progress,
        )

    decision = _release_decision([result.decision for result in task_results])
    finalization = _finalize_release(
        repo_path=config.repo_path,
        integration_branch=feature_branch,
        base_branch=config.default_base_branch,
        decision=decision,
        mode=release_finalize,
        progress=progress,
    )
    summary_path = _write_release_summary(
        runs_dir=runs_dir,
        run_id=run_id,
        release_id=release_id,
        decision=decision,
        task_results=task_results,
        log_path=log_path,
        raw_log_path=raw_log_path,
        integration_branch=feature_branch,
        finalization=finalization,
    )
    metrics_path = _write_release_metrics(
        runs_dir=runs_dir,
        run_id=run_id,
        release_id=release_id,
        decision=decision,
        task_results=task_results,
        raw_log_path=raw_log_path,
    )
    review_path = _write_release_review(
        runs_dir=runs_dir,
        run_id=run_id,
        release_id=release_id,
        decision=decision,
        task_results=task_results,
        integration_branch=feature_branch,
        finalization=finalization,
    )
    _report(progress, f"event=release_decision decision={decision}")
    _report(progress, f"event=release_review path={review_path}")
    _report(progress, f"event=release_metrics path={metrics_path}")
    _write_release_log_summary(
        log_path=log_path,
        raw_log_path=raw_log_path,
        release_id=release_id,
        decision=decision,
        task_results=task_results,
        metrics_path=metrics_path,
        review_path=review_path,
    )

    return ReleaseRunResult(
        release_id=release_id,
        run_id=run_id,
        summary_path=summary_path,
        log_path=log_path,
        review_path=review_path,
        metrics_path=metrics_path,
        task_results=task_results,
        decision=decision,
        integration_branch=feature_branch,
        finalization=finalization,
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


def _run_release_sequential(
    *,
    project_id: str,
    config_repo_path: Path,
    config_dir: Path,
    runs_dir: Path,
    task_base_branch: str,
    task_inputs: list[tuple[Path, TaskContract]],
    executor: ExecutorProtocol | None,
    verification_timeout_seconds: int,
    allow_dirty: bool,
    commit_on_accept: bool,
    merge_on_accept: bool,
    push_on_accept: bool,
    stop_on_failure: bool,
    debug_keep_artifacts: bool,
    progress: Callable[[str], None] | None,
) -> list[TaskRunResult]:
    task_results: list[TaskRunResult] = []
    for index, (contract_path, task) in enumerate(task_inputs, start=1):
        _report(
            progress,
            f"event=task_started index={index} total={len(task_inputs)} task={task.task_id} "
            f"title={json.dumps(task.title)} budget={task.budget_class} type={task.task_type}",
        )
        _report(progress, f"event=task_objective task={task.task_id} objective={json.dumps(task.objective)}")
        _report(progress, f"event=task_scope task={task.task_id} allowed={json.dumps(task.allowed_files)}")
        result = _run_one_release_task(
            project_id=project_id,
            config_repo_path=config_repo_path,
            config_dir=config_dir,
            runs_dir=runs_dir,
            task_base_branch=task_base_branch,
            contract_path=contract_path,
            task=task,
            executor=executor,
            verification_timeout_seconds=verification_timeout_seconds,
            allow_dirty=allow_dirty,
            commit_on_accept=commit_on_accept,
            merge_on_accept=merge_on_accept,
            push_on_accept=push_on_accept,
            debug_keep_artifacts=debug_keep_artifacts,
            progress=progress,
        )
        task_results.append(result)
        if stop_on_failure and result.decision.decision != Decision.ACCEPTED:
            _report(progress, f"stopping release after {task.task_id}: {result.decision.decision}")
            break
    return task_results


def _run_release_parallel(
    *,
    project_id: str,
    config_repo_path: Path,
    config_dir: Path,
    runs_dir: Path,
    task_base_branch: str,
    task_inputs: list[tuple[Path, TaskContract]],
    dependencies: dict[str, list[str]],
    executor: ExecutorProtocol | None,
    verification_timeout_seconds: int,
    allow_dirty: bool,
    commit_on_accept: bool,
    merge_on_accept: bool,
    push_on_accept: bool,
    stop_on_failure: bool,
    debug_keep_artifacts: bool,
    progress: Callable[[str], None] | None,
) -> list[TaskRunResult]:
    by_task_id = {task.task_id: (path, task) for path, task in task_inputs}
    pending = set(by_task_id)
    completed: set[str] = set()
    failed = False
    task_results_by_id: dict[str, TaskRunResult] = {}
    futures: dict[Future[TaskRunResult], str] = {}
    max_workers = max(1, len(task_inputs))
    _report(progress, f"event=parallel_scheduler max_workers={max_workers}")
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        while pending or futures:
            ready = sorted(
                task_id
                for task_id in pending
                if not (stop_on_failure and failed)
                and all(dependency in completed for dependency in dependencies.get(task_id, []))
            )
            for task_id in ready:
                contract_path, task = by_task_id[task_id]
                pending.remove(task_id)
                _report(
                    progress,
                    f"event=task_submitted task={task_id} title={json.dumps(task.title)} "
                    f"budget={task.budget_class} type={task.task_type}",
                )
                _report(progress, f"event=task_objective task={task_id} objective={json.dumps(task.objective)}")
                _report(progress, f"event=task_scope task={task_id} allowed={json.dumps(task.allowed_files)}")
                futures[
                    pool.submit(
                        _run_one_release_task,
                        project_id=project_id,
                        config_repo_path=config_repo_path,
                        config_dir=config_dir,
                        runs_dir=runs_dir,
                        task_base_branch=task_base_branch,
                        contract_path=contract_path,
                        task=task,
                        executor=executor,
                        verification_timeout_seconds=verification_timeout_seconds,
                        allow_dirty=allow_dirty,
                        commit_on_accept=commit_on_accept,
                        merge_on_accept=merge_on_accept,
                        push_on_accept=push_on_accept,
                        debug_keep_artifacts=debug_keep_artifacts,
                        progress=progress,
                    )
                ] = task_id

            if not futures:
                blocked = ", ".join(sorted(pending))
                if failed and stop_on_failure:
                    _report(progress, f"event=parallel_scheduler_stopped pending={json.dumps(blocked)}")
                    break
                raise ValueError(f"release execution DAG has unsatisfied dependencies: {blocked}")

            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                task_id = futures.pop(future)
                result = future.result()
                task_results_by_id[task_id] = result
                completed.add(task_id)
                _report(progress, f"event=task_completed task={task_id} decision={result.decision.decision}")
                if result.decision.decision != Decision.ACCEPTED:
                    failed = True

    return [task_results_by_id[task.task_id] for _, task in task_inputs if task.task_id in task_results_by_id]


def _run_one_release_task(
    *,
    project_id: str,
    config_repo_path: Path,
    config_dir: Path,
    runs_dir: Path,
    task_base_branch: str,
    contract_path: Path,
    task: TaskContract,
    executor: ExecutorProtocol | None,
    verification_timeout_seconds: int,
    allow_dirty: bool,
    commit_on_accept: bool,
    merge_on_accept: bool,
    push_on_accept: bool,
    debug_keep_artifacts: bool,
    progress: Callable[[str], None] | None,
) -> TaskRunResult:
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
        base_branch=task_base_branch,
        progress=progress,
    )
    if debug_keep_artifacts:
        _report(progress, f"debug_keep_artifacts=true worktree={result.worktree_path}")
    else:
        for message in cleanup_task_artifacts(
            repo_path=config_repo_path,
            worktree_path=result.worktree_path,
            branch=f"agent/{task.release_id}/{task.task_id}",
            preserve_worktree=_should_preserve_task_worktree(result),
            preserve_branch=_should_preserve_task_branch(result),
        ):
            _report(progress, message)
    return result


def _load_release_plan(config_repo_path: Path, repo_state_path: Path | None) -> ReleasePlan | None:
    if repo_state_path is None:
        return None
    root = repo_state_path if repo_state_path.is_absolute() else config_repo_path / repo_state_path
    path = root / "release_plan.yaml"
    if not path.exists():
        return None
    return load_yaml_model(path, ReleasePlan)


def _finalize_release(
    *,
    repo_path: Path,
    integration_branch: str,
    base_branch: str,
    decision: Decision,
    mode: str,
    progress: Callable[[str], None] | None,
) -> FinalizeResult | None:
    if mode == "none":
        return None
    if decision != Decision.ACCEPTED:
        _report(progress, f"release_finalization_skipped decision={decision}")
        return None
    try:
        if mode == "push-feature":
            push_branch(repo_path, integration_branch)
            _report(progress, f"event=release_pushed branch=origin/{integration_branch}")
            return FinalizeResult(pushed=True)
        if mode in {"merge-main", "push-main"}:
            result = merge_integration_branch_to_base(
                repo_path=repo_path,
                integration_branch=integration_branch,
                base_branch=base_branch,
                push=mode == "push-main",
            )
            _report(progress, f"event=release_merged target={base_branch}")
            if result.pushed:
                _report(progress, f"event=release_pushed branch=origin/{base_branch}")
            return result
    except GitFinalizeError as error:
        _report(progress, f"event=release_finalization_failed error={json.dumps(str(error))}")
        return FinalizeResult(failed_step=error.step, error=str(error))
    raise ValueError(f"unsupported release finalization mode: {mode}")


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


def _ensure_no_existing_task_branches(
    repo_path: Path,
    release_id: str,
    tasks: list[TaskContract],
) -> None:
    existing: list[str] = []
    for task in tasks:
        branch = branch_name(release_id, task.task_id)
        result = run_process(
            ["git", "branch", "--list", branch],
            cwd=repo_path,
            timeout_seconds=30,
        )
        if result.exit_code != 0:
            raise ValueError(
                "could not inspect existing release task branches: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        if result.stdout.strip():
            existing.append(branch)

    if not existing:
        return
    listed = ", ".join(existing[:5])
    suffix = "" if len(existing) <= 5 else f", ... (+{len(existing) - 5} more)"
    raise ValueError(
        "release task branches already exist before release start: "
        f"{listed}{suffix}. Merge or delete stale branches before running run-release."
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
    raw_log_path: Path,
    integration_branch: str,
    finalization: FinalizeResult | None,
) -> Path:
    summary_dir = runs_dir / run_id
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summary_dir / "release_summary.json"
    summary = {
        "run_id": run_id,
        "release_id": release_id,
        "decision": decision,
        "log_path": str(log_path),
        "raw_log_path": str(raw_log_path),
        "integration_branch": integration_branch,
        "finalization": {
            "merged": finalization.merged,
            "pushed": finalization.pushed,
            "failed_step": finalization.failed_step,
            "error": finalization.error,
        }
        if finalization
        else None,
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


def _write_release_review(
    *,
    runs_dir: Path,
    run_id: str,
    release_id: str,
    decision: Decision,
    task_results: list[TaskRunResult],
    integration_branch: str,
    finalization: FinalizeResult | None,
) -> Path:
    review_path = runs_dir / run_id / "release_review.md"
    lines = [
        f"# Release Review: {release_id}",
        "",
        f"- Decision: `{decision}`",
        f"- Integration branch: `{integration_branch}`",
        f"- Tasks completed: `{len(task_results)}`",
        "",
        "## Task Results",
        "",
    ]
    for result in task_results:
        finalize = result.finalize
        lines.extend(
            [
                f"### {result.decision.task_id}",
                "",
                f"- Decision: `{result.decision.decision}`",
                f"- Rationale: {result.decision.rationale}",
                f"- Evidence: `{result.bundle_path}`",
                f"- Commit: `{finalize.commit_hash if finalize and finalize.commit_hash else 'none'}`",
                f"- Merged: `{bool(finalize and finalize.merged)}`",
                f"- Pushed: `{bool(finalize and finalize.pushed)}`",
                "",
            ]
        )
        if result.decision.risks:
            lines.append("Risks:")
            lines.extend(f"- {risk}" for risk in result.decision.risks)
            lines.append("")
        if result.decision.follow_up_tasks:
            lines.append("Follow-up tasks:")
            lines.extend(f"- {task}" for task in result.decision.follow_up_tasks)
            lines.append("")
    if any(result.decision.decision != Decision.ACCEPTED for result in task_results):
        lines.extend(
            [
                "## Release Follow-Up",
                "",
                "- Inspect non-accepted task evidence before rerunning or expanding the release.",
                "",
            ]
        )
    if finalization is not None:
        lines.extend(
            [
                "## Release Finalization",
                "",
                f"- Merged: `{finalization.merged}`",
                f"- Pushed: `{finalization.pushed}`",
                f"- Error: `{finalization.error or 'none'}`",
                "",
            ]
        )
    review_path.write_text("\n".join(lines), encoding="utf-8")
    return review_path


def _write_release_metrics(
    *,
    runs_dir: Path,
    run_id: str,
    release_id: str,
    decision: Decision,
    task_results: list[TaskRunResult],
    raw_log_path: Path,
) -> Path:
    metrics_path = runs_dir / run_id / "release_metrics.json"
    task_metrics = [_task_metrics(result, raw_log_path) for result in task_results]
    model_attempts: dict[str, dict[str, object]] = {}
    for task in task_metrics:
        for attempt in task["executor_attempts"]:
            model = str(attempt.get("model") or "<none>")
            entry = model_attempts.setdefault(
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
            if int(attempt.get("exit_code", 1)) == 0:
                entry["successful_attempts"] = int(entry["successful_attempts"]) + 1
            else:
                entry["failed_attempts"] = int(entry["failed_attempts"]) + 1
            entry["duration_seconds"] = float(entry["duration_seconds"]) + float(attempt.get("duration_seconds", 0.0))
            entry["prompt_chars"] = int(entry["prompt_chars"]) + int(attempt.get("prompt_chars", 0))
            entry["stdout_chars"] = int(entry["stdout_chars"]) + int(attempt.get("stdout_chars", 0))
            entry["stderr_chars"] = int(entry["stderr_chars"]) + int(attempt.get("stderr_chars", 0))

    totals = {
        "tasks": len(task_metrics),
        "accepted_tasks": sum(1 for task in task_metrics if task["decision"] == Decision.ACCEPTED),
        "executor_attempts": sum(len(task["executor_attempts"]) for task in task_metrics),
        "prompt_chars": sum(int(task["prompt_chars"]) for task in task_metrics),
        "context_chars": sum(int(task["context_chars"]) for task in task_metrics),
        "stdout_chars": sum(int(task["stdout_chars"]) for task in task_metrics),
        "stderr_chars": sum(int(task["stderr_chars"]) for task in task_metrics),
        "diff_lines": sum(int(task["diff_lines"]) for task in task_metrics),
        "changed_files": sum(int(task["changed_file_count"]) for task in task_metrics),
        "verification_duration_seconds": round(
            sum(float(task["verification_duration_seconds"]) for task in task_metrics),
            3,
        ),
        "executor_duration_seconds": round(
            sum(float(attempt.get("duration_seconds", 0.0)) for task in task_metrics for attempt in task["executor_attempts"]),
            3,
        ),
    }
    metrics = {
        "run_id": run_id,
        "release_id": release_id,
        "decision": decision,
        "totals": totals,
        "model_attempts": model_attempts,
        "tasks": task_metrics,
        "notes": [
            "Character counts are local proxies for cost analysis; token counts require provider usage metadata.",
            "prompt_chars includes the full executor prompt, while context_chars tracks only context bundle content reported by orchestration.",
        ],
    }
    metrics_path.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    return metrics_path


def _write_release_log_summary(
    *,
    log_path: Path,
    raw_log_path: Path,
    release_id: str,
    decision: Decision,
    task_results: list[TaskRunResult],
    metrics_path: Path,
    review_path: Path,
) -> None:
    task_lines = []
    for result in task_results:
        finalize = result.finalize
        commit = finalize.commit_hash[:12] if finalize and finalize.commit_hash else "none"
        merged = bool(finalize and finalize.merged)
        task_lines.append(
            f"- {result.decision.task_id}: {result.decision.decision} "
            f"(commit={commit}, merged={merged})"
        )
    summary_lines = [
        "",
        "=== Release Summary ===",
        f"Release: {release_id}",
        f"Decision: {decision}",
        f"Tasks completed: {len(task_results)}",
        *task_lines,
        f"Review: {review_path}",
        f"Metrics: {metrics_path}",
        "Good luck, future humans. 🧑‍🚀🛠️🍀",
    ]
    timestamp = datetime.now(UTC).isoformat()
    payload = "".join(f"{timestamp} {line}\n" if line else "\n" for line in summary_lines)
    with log_path.open("a", encoding="utf-8") as file:
        file.write(payload)
    with raw_log_path.open("a", encoding="utf-8") as file:
        file.write(payload)


def _task_metrics(result: TaskRunResult, raw_log_path: Path) -> dict[str, object]:
    bundle_path = result.bundle_path
    attempts = _read_json_list(bundle_path / "executor_attempts.json")
    run_state = _read_json_object(bundle_path / "run_state.json")
    prompt_chars = _text_len(bundle_path / "executor_prompt.md")
    changed_files = _read_lines(bundle_path / "changed_files.txt")
    verification_results = run_state.get("verification_results", [])
    return {
        "task_id": result.decision.task_id,
        "run_id": result.run_id,
        "decision": result.decision.decision,
        "rationale": result.decision.rationale,
        "bundle_path": str(bundle_path),
        "commit_hash": result.finalize.commit_hash if result.finalize else None,
        "merged": result.finalize.merged if result.finalize else False,
        "pushed": result.finalize.pushed if result.finalize else False,
        "context_chars": _context_chars_for_task(raw_log_path, result.decision.task_id),
        "prompt_chars": prompt_chars,
        "stdout_chars": sum(int(attempt.get("stdout_chars", 0)) for attempt in attempts),
        "stderr_chars": sum(int(attempt.get("stderr_chars", 0)) for attempt in attempts),
        "diff_lines": int(run_state.get("diff_lines", 0)),
        "changed_file_count": len(changed_files),
        "changed_files": changed_files,
        "verification_command_count": len(verification_results),
        "verification_duration_seconds": round(
            sum(float(item.get("duration_seconds", 0.0)) for item in verification_results),
            3,
        ),
        "executor_attempts": attempts,
        "failure_diagnosis_path": str(bundle_path / "failure_diagnosis.yaml")
        if (bundle_path / "failure_diagnosis.yaml").exists()
        else None,
    }


def _read_json_object(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _read_json_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _text_len(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8"))


def _context_chars_for_task(raw_log_path: Path, task_id: str) -> int:
    if not raw_log_path.exists():
        return 0
    marker = f"event=context_loaded task={task_id} "
    for line in raw_log_path.read_text(encoding="utf-8").splitlines():
        if marker not in line:
            continue
        for part in line.split():
            if part.startswith("chars="):
                try:
                    return int(part.removeprefix("chars="))
                except ValueError:
                    return 0
    return 0


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


def _release_dependency_map(
    tasks: list[TaskContract],
    overlap_report: ReleaseOverlapReport,
) -> dict[str, list[str]]:
    task_ids = {task.task_id for task in tasks}
    dependencies: dict[str, set[str]] = {task.task_id: set(task.depends_on) for task in tasks}
    for task in tasks:
        unknown = sorted(set(task.depends_on) - task_ids)
        if unknown:
            raise ValueError(
                f"task {task.task_id} depends on unknown release task(s): {', '.join(unknown)}"
            )
    for finding in overlap_report.findings:
        if finding.severity in {"minor", "broad"}:
            dependencies[finding.second_task_id].add(finding.first_task_id)
    return {
        task_id: sorted(values)
        for task_id, values in dependencies.items()
        if values
    }


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
    raw_log_path: Path,
) -> Callable[[str], None]:
    lock = threading.Lock()

    def report(message: str) -> None:
        timestamp = datetime.now(UTC).isoformat()
        raw_line = f"{timestamp} {message}\n"
        display_message = _display_progress_message(message)
        with lock:
            raw_log_path.parent.mkdir(parents=True, exist_ok=True)
            with raw_log_path.open("a", encoding="utf-8") as file:
                file.write(raw_line)
            if display_message is not None:
                with log_path.open("a", encoding="utf-8") as file:
                    file.write(f"{timestamp} {display_message}\n")
        if progress is not None and display_message is not None:
            progress(display_message)

    return report


def _display_progress_message(message: str) -> str | None:
    if message.startswith("event="):
        return _display_event_message(message)
    if not message.startswith("agent "):
        return message
    if " stream=stdout | " in message:
        return _truncate_log_line(message)
    if "ERROR:" in message or "usage limit" in message.lower():
        return _truncate_log_line(message)
    return None


def _display_event_message(message: str) -> str:
    event = _event_fields(message)
    name = event.get("event", "event")
    if name == "release_started":
        return f"Release {event.get('release')} started: {event.get('tasks')} task(s), mode={event.get('mode')}, run={event.get('run_id')}"
    if name == "release_logs":
        return f"Logs: {event.get('log')} (raw: {event.get('raw_log')})"
    if name == "integration_branch":
        return f"Integration branch ready: {event.get('branch')} from {event.get('base')}"
    if name == "execution_dag":
        return f"Execution DAG: {event.get('dependencies')}"
    if name == "task_started":
        return f"Task {event.get('index')}/{event.get('total')} {event.get('task')}: {event.get('title')} [{event.get('type')}, budget {event.get('budget')}]"
    if name == "task_submitted":
        return f"Task submitted {event.get('task')}: {event.get('title')} [{event.get('type')}, budget {event.get('budget')}]"
    if name == "task_objective":
        return f"Task {event.get('task')} objective: {event.get('objective')}"
    if name == "task_scope":
        return f"Task {event.get('task')} scope: {event.get('allowed')}"
    if name == "task_run_created":
        return f"Task {event.get('task')} run created: {event.get('run_id')}"
    if name == "worktree_created":
        return f"Task {event.get('task')} worktree: {event.get('path')}"
    if name == "prompt_build_started":
        return f"Task {event.get('task')} building executor prompt"
    if name == "context_loaded":
        return f"Task {event.get('task')} context loaded: {event.get('sections')} section(s), {event.get('chars')} chars"
    if name == "executor_attempt_started":
        return f"Task {event.get('task')} executor attempt {event.get('attempt')}/{event.get('total')}: {event.get('backend')} model={event.get('model')}"
    if name == "executor_attempt_finished":
        return f"Task {event.get('task')} executor attempt {event.get('attempt')} finished: exit={event.get('exit_code')}"
    if name == "executor_finished":
        return f"Task {event.get('task')} executor finished: exit={event.get('exit_code')}"
    if name == "verification_started":
        return f"Task {event.get('task')} verification started: {event.get('commands')} command(s)"
    if name == "verification_finished":
        return f"Task {event.get('task')} verification finished: exit_codes={event.get('exit_codes')}"
    if name == "evidence_collection_started":
        return f"Task {event.get('task')} collecting evidence"
    if name == "review_decision":
        return f"Task {event.get('task')} review: {event.get('decision')} - {event.get('rationale')}"
    if name == "task_finalization_started":
        return f"Task {event.get('task')} finalizing accepted changes"
    if name == "task_committed":
        return f"Task {event.get('task')} committed: {event.get('commit')}"
    if name == "task_merged":
        return f"Task {event.get('task')} merged into {event.get('target')}"
    if name == "task_pushed":
        return f"Task {event.get('task')} pushed: {event.get('branch')}"
    if name == "task_completed":
        return f"Task {event.get('task')} completed: {event.get('decision')}"
    if name == "failure_diagnosis":
        return f"Task {event.get('task')} failure diagnosis: {event.get('category')}"
    if name == "conflict_repair_started":
        return f"Task {event.get('task')} conflict repair started: {event.get('files')} file(s)"
    if name == "release_decision":
        return f"Release decision: {event.get('decision')}"
    if name == "release_review":
        return f"Release review: {event.get('path')}"
    if name == "release_metrics":
        return f"Release metrics: {event.get('path')}"
    if name == "release_pushed":
        return f"Release pushed: {event.get('branch')}"
    if name == "release_merged":
        return f"Release merged into {event.get('target')}"
    return message


def _event_fields(message: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    try:
        parts = shlex.split(message)
    except ValueError:
        parts = message.split()
    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[key] = value
    return fields


def _truncate_log_line(message: str, limit: int = 500) -> str:
    if len(message) <= limit:
        return message
    return message[: limit - 15] + "... [truncated]"
