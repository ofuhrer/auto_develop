from __future__ import annotations

import json
import os
import shlex
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from agentic_devloop.artifacts import cleanup_task_artifacts
from agentic_devloop.budget import build_budget_ledger, build_tuning_report
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
    ReleaseRunState,
    ReleaseState,
    ReleaseOverlapReport,
    ReleasePlan,
    ReviewDecision,
    TaskContract,
)
from agentic_devloop.orchestrator import ExecutorProtocol, TaskRunResult, branch_name, run_task
from agentic_devloop.process import run_process
from agentic_devloop.runtime_state import write_json
from agentic_devloop.security import redact_text, validate_identifier
from agentic_devloop.yaml_io import load_yaml_model

MAX_PARALLEL_RELEASE_WORKERS = 4


@dataclass(frozen=True)
class ReleaseRunResult:
    release_id: str
    run_id: str
    summary_path: Path
    log_path: Path
    review_path: Path
    metrics_path: Path
    budget_path: Path
    tuning_path: Path
    task_results: list[TaskRunResult]
    decision: Decision
    integration_branch: str | None = None
    finalization: FinalizeResult | None = None


def make_release_run_id(release_id: str, now: datetime | None = None) -> str:
    validate_identifier(release_id, kind="release_id")
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{release_id}_release"


def feature_branch_name(release_id: str) -> str:
    validate_identifier(release_id, kind="release_id")
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
    validate_identifier(release_id, kind="release_id")
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
    release_state = ReleaseRunState(
        release_id=release_id,
        run_id=run_id,
        state=ReleaseState.STARTED,
        started_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        integration_branch=feature_branch,
    )
    release_state_path = release_root / "release_state.json"
    _persist_release_state(release_state_path, release_state)
    try:
        ensure_branch_from_base(config.repo_path, feature_branch, config.default_base_branch)
        release_state = _update_release_state(release_state, state=ReleaseState.RUNNING)
        _persist_release_state(release_state_path, release_state)
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
        release_state = _update_release_state(
            release_state,
            task_states={result.decision.task_id: str(result.decision.decision) for result in task_results},
        )
        _persist_release_state(release_state_path, release_state)

        task_decision = _release_decision([result.decision for result in task_results])
        release_metrics = _build_release_metrics(
            run_id=run_id,
            release_id=release_id,
            decision=task_decision,
            task_results=task_results,
            raw_log_path=raw_log_path,
            runs_dir=runs_dir,
        )
        metrics_path = _write_release_metrics(
            runs_dir=runs_dir,
            run_id=run_id,
            metrics=release_metrics,
        )
        budget_ledger = build_budget_ledger(release_metrics=release_metrics, budget=config.budget)
        budget_path = _write_release_budget(runs_dir=runs_dir, run_id=run_id, ledger=budget_ledger)
        tuning_path = _write_release_tuning(
            runs_dir=runs_dir,
            run_id=run_id,
            tuning_report=build_tuning_report(ledger=budget_ledger),
        )
        budget_violations = _release_budget_violations(budget_ledger)
        decision = _release_decision_with_budget(task_decision, budget_violations)
        if decision != task_decision:
            release_metrics["decision"] = decision
            release_metrics["budget_violations"] = budget_violations
            metrics_path = _write_release_metrics(runs_dir=runs_dir, run_id=run_id, metrics=release_metrics)
            _report(progress, "event=release_budget_exceeded violations=" + json.dumps(budget_violations, sort_keys=True))
        release_state = _update_release_state(release_state, state=ReleaseState.FINALIZING, decision=decision)
        _persist_release_state(release_state_path, release_state)
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
            budget_path=budget_path,
            tuning_path=tuning_path,
            budget_violations=budget_violations,
        )
        review_path = _write_release_review(
            runs_dir=runs_dir,
            run_id=run_id,
            release_id=release_id,
            decision=decision,
            task_results=task_results,
            integration_branch=feature_branch,
            finalization=finalization,
            metrics_path=metrics_path,
            budget_path=budget_path,
            tuning_path=tuning_path,
            budget_violations=budget_violations,
        )
        release_state = _update_release_state(
            release_state,
            state=_release_state_for_decision(decision),
            decision=decision,
            summary_path=summary_path,
            review_path=review_path,
            metrics_path=metrics_path,
            budget_path=budget_path,
            tuning_path=tuning_path,
        )
        _persist_release_state(release_state_path, release_state)
        _report(progress, f"event=release_decision decision={decision}")
        _report(progress, f"event=release_review path={review_path}")
        _report(progress, f"event=release_metrics path={metrics_path}")
        _report(progress, f"event=release_budget path={budget_path}")
        _report(progress, f"event=release_tuning path={tuning_path}")
        _write_release_log_summary(
            log_path=log_path,
            raw_log_path=raw_log_path,
            release_id=release_id,
            decision=decision,
            task_results=task_results,
            metrics_path=metrics_path,
            budget_path=budget_path,
            tuning_path=tuning_path,
            review_path=review_path,
        )
    except KeyboardInterrupt:
        _persist_release_state(release_state_path, _update_release_state(release_state, state=ReleaseState.INTERRUPTED))
        raise

    return ReleaseRunResult(
        release_id=release_id,
        run_id=run_id,
        summary_path=summary_path,
        log_path=log_path,
        review_path=review_path,
        metrics_path=metrics_path,
        budget_path=budget_path,
        tuning_path=tuning_path,
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
    max_workers = max(1, min(len(task_inputs), os.cpu_count() or 1, MAX_PARALLEL_RELEASE_WORKERS))
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
    budget_path: Path,
    tuning_path: Path,
    budget_violations: list[str],
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
        "metrics_path": str(summary_dir / "release_metrics.json"),
        "budget_path": str(budget_path),
        "tuning_path": str(tuning_path),
        "budget_violations": budget_violations,
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
    metrics_path: Path,
    budget_path: Path,
    tuning_path: Path,
    budget_violations: list[str],
) -> Path:
    review_path = runs_dir / run_id / "release_review.md"
    lines = [
        f"# Release Review: {release_id}",
        "",
        f"- Decision: `{decision}`",
        f"- Integration branch: `{integration_branch}`",
        f"- Tasks completed: `{len(task_results)}`",
        f"- Metrics: `{metrics_path}`",
        f"- Budget: `{budget_path}`",
        f"- Tuning: `{tuning_path}`",
        "",
        "## Budget",
        "",
    ]
    if budget_violations:
        lines.extend(f"- Violation: {violation}" for violation in budget_violations)
    else:
        lines.append("- No configured release-level budget limits were exceeded.")
    lines.extend([
        "",
        "## Task Results",
        "",
    ])
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


def _build_release_metrics(
    *,
    run_id: str,
    release_id: str,
    decision: Decision,
    task_results: list[TaskRunResult],
    raw_log_path: Path,
    runs_dir: Path,
) -> dict[str, object]:
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
        "strong_model_calls": _strong_model_calls(runs_dir, release_id),
        "model_attempts": model_attempts,
        "tasks": task_metrics,
        "notes": [
            "Character counts are local proxies for cost analysis; token counts require provider usage metadata.",
            "prompt_chars includes the full executor prompt, while context_chars tracks only context bundle content reported by orchestration.",
        ],
    }
    return metrics


def _write_release_metrics(
    *,
    runs_dir: Path,
    run_id: str,
    metrics: dict[str, object],
) -> Path:
    metrics_path = runs_dir / run_id / "release_metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    return metrics_path


def _write_release_budget(*, runs_dir: Path, run_id: str, ledger) -> Path:
    budget_path = runs_dir / run_id / "release_budget.json"
    budget_path.write_text(json.dumps(ledger.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return budget_path


def _write_release_tuning(*, runs_dir: Path, run_id: str, tuning_report) -> Path:
    tuning_path = runs_dir / run_id / "release_tuning.md"
    tuning_path.write_text(tuning_report.render_markdown(), encoding="utf-8")
    return tuning_path


def _release_budget_violations(ledger) -> list[str]:
    return [
        f"{entry.name} exceeded budget: actual {entry.actual} {entry.unit} over configured {entry.configured}"
        for entry in ledger.usage
        if entry.scope == "release" and entry.over_by is not None and entry.over_by > 0
    ]


def _release_decision_with_budget(decision: Decision, budget_violations: list[str]) -> Decision:
    if budget_violations and decision == Decision.ACCEPTED:
        return Decision.FAILED
    return decision


def _write_release_log_summary(
    *,
    log_path: Path,
    raw_log_path: Path,
    release_id: str,
    decision: Decision,
    task_results: list[TaskRunResult],
    metrics_path: Path,
    budget_path: Path,
    tuning_path: Path,
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
        _style("🧾 Release Summary", "bold"),
        f"🚀 Release: {_style(release_id, 'cyan')}",
        f"{_status_icon(str(decision))} Decision: {_style(str(decision), _decision_style(str(decision)))}",
        f"📦 Tasks completed: {len(task_results)}",
        *task_lines,
        f"🧑‍⚖️ Review: {_style(str(review_path), 'dim')}",
        f"📊 Metrics: {_style(str(metrics_path), 'dim')}",
        f"💰 Budget: {_style(str(budget_path), 'dim')}",
        f"🛠️ Tuning: {_style(str(tuning_path), 'dim')}",
        _style("Good luck, future humans. 🧑‍🚀🛠️🍀", "green"),
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


def _strong_model_calls(runs_dir: Path, release_id: str) -> int:
    ledger_path = runs_dir / release_id / "budget_ledger.json"
    return sum(1 for entry in _read_json_list(ledger_path) if entry.get("kind") == "strong_model")


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


def _update_release_state(release_state: ReleaseRunState, **updates) -> ReleaseRunState:
    return release_state.model_copy(update={"updated_at": datetime.now(UTC), **updates})


def _persist_release_state(path: Path, release_state: ReleaseRunState) -> None:
    write_json(path, release_state.model_dump(mode="json"))


def _release_state_for_decision(decision: Decision) -> ReleaseState:
    if decision == Decision.ACCEPTED:
        return ReleaseState.ACCEPTED
    if decision == Decision.ESCALATED:
        return ReleaseState.ESCALATED
    return ReleaseState.FAILED


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
    formatter = _HumanLogFormatter()

    def report(message: str) -> None:
        timestamp = datetime.now(UTC).isoformat()
        safe_message = redact_text(message)
        raw_line = f"{timestamp} {safe_message}\n"
        display_messages = formatter.format(safe_message)
        with lock:
            raw_log_path.parent.mkdir(parents=True, exist_ok=True)
            with raw_log_path.open("a", encoding="utf-8") as file:
                file.write(raw_line)
            if display_messages:
                with log_path.open("a", encoding="utf-8") as file:
                    for display_message in display_messages:
                        file.write(f"{timestamp} {display_message}\n")
        if progress is not None:
            for display_message in display_messages:
                progress(display_message)

    return report


class _HumanLogFormatter:
    def __init__(self) -> None:
        self._active_worker_sections: dict[tuple[str, str], str] = {}

    def format(self, message: str) -> list[str]:
        if message.startswith("event="):
            return [_display_event_message(message)]
        if message.startswith("agent "):
            return self._format_agent_message(message)
        return [_style(f"ℹ️  {message}", "dim")]

    def _format_agent_message(self, message: str) -> list[str]:
        metadata, separator, content = message.partition(" | ")
        if not separator:
            return []
        fields = _agent_fields(metadata)
        if fields.get("stream") != "stdout":
            return []
        task = fields.get("task", "?")
        attempt = fields.get("attempt", "?")
        key = (task, attempt)
        text = _clean_worker_line(content)
        if not text:
            return []

        heading = _worker_heading(text)
        if heading is not None:
            self._active_worker_sections[key] = heading
            return [_style(f"📝 {task} worker summary: {heading}", "cyan")]

        section = self._active_worker_sections.get(key)
        if section is not None:
            if text.startswith(("-", "*")) or text.startswith("`") or "passed" in text.lower() or "failed" in text.lower():
                return [f"   {_style(_shorten_worker_paths(text), 'dim')}"]
            return [f"   {_shorten_worker_paths(text)}"]

        if _looks_like_worker_summary(text):
            return [_style(f"🤖 {task}: {_truncate_log_line(text, 180)}", "cyan")]
        return []


def _display_event_message(message: str) -> str:
    event = _event_fields(message)
    name = event.get("event", "event")
    if name == "release_started":
        return _style(
            f"🚀 Release {event.get('release')} started: {event.get('tasks')} task(s), mode={event.get('mode')}, run={event.get('run_id')}",
            "bold",
        )
    if name == "release_logs":
        return f"📡 Watching: {_style(str(event.get('log')), 'cyan')}  {_style('(raw audit: ' + str(event.get('raw_log')) + ')', 'dim')}"
    if name == "integration_branch":
        return f"🌿 Integration branch ready: {_style(str(event.get('branch')), 'cyan')} from {event.get('base')}"
    if name == "execution_dag":
        return f"🕸️ Execution DAG: {_style(_compact_value(str(event.get('dependencies'))), 'dim')}"
    if name == "overlap_findings":
        return f"🧩 Contract overlap findings: {event.get('count')}  {_style('scheduler will serialize risky overlap', 'dim')}"
    if name == "task_started":
        return _style(
            f"🧭 Task {event.get('index')}/{event.get('total')} {event.get('task')}: {event.get('title')} [{event.get('type')}, budget {event.get('budget')}]",
            "bold",
        )
    if name == "task_submitted":
        return f"🧭 Task submitted {event.get('task')}: {event.get('title')} [{event.get('type')}, budget {event.get('budget')}]"
    if name == "task_objective":
        return f"🎯 {event.get('task')} objective: {_truncate_log_line(str(event.get('objective')), 260)}"
    if name == "task_scope":
        return f"🗂️ {event.get('task')} scope: {_compact_value(str(event.get('allowed')))}"
    if name == "task_run_created":
        return f"🧪 {event.get('task')} run: {_style(str(event.get('run_id')), 'dim')}"
    if name == "worktree_created":
        return f"🌱 {event.get('task')} worktree created: {_style(_short_path(str(event.get('path'))), 'dim')}"
    if name == "prompt_build_started":
        return f"🧵 {event.get('task')} building executor prompt"
    if name == "context_loaded":
        return f"📚 {event.get('task')} context loaded: {event.get('sections')} section(s), {event.get('chars')} chars"
    if name == "executor_attempt_started":
        return f"🤖 {event.get('task')} worker started: attempt {event.get('attempt')}/{event.get('total')} on {_style(str(event.get('model')), 'cyan')}"
    if name == "executor_heartbeat":
        elapsed = _format_duration(int(event.get("elapsed_seconds", "0")))
        return _style(
            f"🤖 {event.get('task')} still working after {elapsed} on {event.get('model')}. Inspect raw log before poking the goblin.",
            "yellow",
        )
    if name == "executor_attempt_finished":
        style = "green" if event.get("exit_code") == "0" else "yellow"
        return _style(f"{_status_icon(str(event.get('exit_code')))} {event.get('task')} worker attempt {event.get('attempt')} finished: exit={event.get('exit_code')}", style)
    if name == "executor_finished":
        style = "green" if event.get("exit_code") == "0" else "red"
        return _style(f"{_status_icon(str(event.get('exit_code')))} {event.get('task')} worker finished: exit={event.get('exit_code')}", style)
    if name == "verification_started":
        return f"🔎 {event.get('task')} verification started: {event.get('commands')} command(s)"
    if name == "verification_finished":
        style = "green" if str(event.get("exit_codes", "")).replace(",", "").strip("0") == "" else "red"
        return _style(f"{_status_icon(str(event.get('exit_codes')))} {event.get('task')} verification finished: exit_codes={event.get('exit_codes')}", style)
    if name == "evidence_collection_started":
        return f"🧾 {event.get('task')} collecting evidence"
    if name == "review_decision":
        decision = str(event.get("decision"))
        line = f"{_status_icon(decision)} {event.get('task')} review: {decision} - {event.get('rationale')}"
        if decision != "accepted":
            line += _style("  ⚠️ Human should inspect evidence before continuing.", "yellow")
        return _style(line, _decision_style(decision))
    if name == "task_finalization_started":
        return f"📦 {event.get('task')} finalizing accepted changes"
    if name == "task_committed":
        return _style(f"✅ {event.get('task')} committed: {str(event.get('commit'))[:12]}", "green")
    if name == "task_merged":
        return _style(f"🔀 {event.get('task')} merged into {event.get('target')}", "green")
    if name == "task_pushed":
        return _style(f"📤 {event.get('task')} pushed: {event.get('branch')}", "green")
    if name == "task_completed":
        return _style(f"✅ {event.get('task')} completed: {event.get('decision')}", _decision_style(str(event.get("decision"))))
    if name == "failure_diagnosis":
        return _style(f"🩺 {event.get('task')} failure diagnosis: {event.get('category')}", "yellow")
    if name == "conflict_repair_started":
        return _style(f"🧯 {event.get('task')} conflict repair started: {event.get('files')} file(s)", "yellow")
    if name == "release_decision":
        return _style(f"{_status_icon(str(event.get('decision')))} Release decision: {event.get('decision')}", _decision_style(str(event.get("decision"))))
    if name == "release_review":
        return f"🧑‍⚖️ Release review: {_style(str(event.get('path')), 'dim')}"
    if name == "release_metrics":
        return f"📊 Release metrics: {_style(str(event.get('path')), 'dim')}"
    if name == "release_budget":
        return f"💰 Release budget: {_style(str(event.get('path')), 'dim')}"
    if name == "release_tuning":
        return f"🛠️ Release tuning: {_style(str(event.get('path')), 'dim')}"
    if name == "release_budget_exceeded":
        return _style(f"⚠️ Release budget exceeded: {_compact_value(str(event.get('violations')))}. Stop and inspect budget/tuning artifacts.", "yellow")
    if name == "release_pushed":
        return _style(f"📤 Release pushed: {event.get('branch')}", "green")
    if name == "release_merged":
        return _style(f"🔀 Release merged into {event.get('target')}", "green")
    return _style(f"ℹ️  {message}", "dim")


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


def _agent_fields(metadata: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in metadata.split():
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key] = value
    return fields


ANSI_STYLES = {
    "bold": "\033[1m",
    "cyan": "\033[36m",
    "dim": "\033[2m",
    "green": "\033[32m",
    "red": "\033[31m",
    "yellow": "\033[33m",
}
ANSI_RESET = "\033[0m"


def _style(text: str, style: str) -> str:
    prefix = ANSI_STYLES.get(style)
    if prefix is None:
        return text
    return f"{prefix}{text}{ANSI_RESET}"


def _decision_style(value: str) -> str:
    if value in {"accepted", "0", "None"}:
        return "green"
    if value in {"failed", "escalated"}:
        return "red"
    return "yellow"


def _status_icon(value: str) -> str:
    normalized = value.lower()
    if normalized in {"accepted", "0"} or set(normalized.replace(",", "")) <= {"0"}:
        return "✅"
    if normalized in {"failed", "escalated"}:
        return "🛑"
    if normalized in {"needs_revision", "needs-revision"}:
        return "⚠️"
    return "⚠️"


def _short_path(value: str) -> str:
    marker = "/auto_develop/"
    if marker in value:
        return "…/auto_develop/" + value.split(marker, 1)[1]
    return value


def _shorten_worker_paths(value: str) -> str:
    value = value.replace("](/Users/fuhrer/Desktop/auto_develop/worktrees/", "](…/worktrees/")
    value = value.replace("/Users/fuhrer/Desktop/auto_develop/worktrees/", "…/worktrees/")
    value = value.replace("/Users/fuhrer/Desktop/auto_develop/main/", "…/main/")
    return value


def _compact_value(value: str, limit: int = 220) -> str:
    compact = value.replace("\\n", " ").replace("[", "").replace("]", "").replace('"', "")
    compact = " ".join(compact.split())
    return _truncate_log_line(compact, limit)


def _format_duration(seconds: int) -> str:
    minutes, remainder = divmod(max(0, seconds), 60)
    if minutes:
        return f"{minutes}m {remainder}s"
    return f"{remainder}s"


USEFUL_WORKER_HEADINGS = {
    "changed files": "Files changed",
    "files changed": "Files changed",
    "what changed": "What changed",
    "result": "Result",
    "verification": "Verification",
    "verification commands run": "Verification",
    "verification result": "Verification result",
    "risks": "Risks",
    "risks / follow-up": "Risks / follow-up",
    "follow-up": "Follow-up",
    "notes": "Notes",
    "documentation review summary": "Documentation review summary",
}


def _worker_heading(line: str) -> str | None:
    normalized = line.strip().strip("*").strip(":").lower()
    return USEFUL_WORKER_HEADINGS.get(normalized)


def _looks_like_worker_summary(line: str) -> bool:
    lowered = line.lower()
    return lowered.startswith(("implemented ", "updated ", "added ", "fixed ", "created "))


def _clean_worker_line(line: str) -> str:
    text = line.strip()
    if not text:
        return ""
    if text.startswith("```"):
        return ""
    if text.startswith(("exec ", "codex ", "apply patch", "/bin/")):
        return ""
    if " WARN codex_" in text or "codex_core_plugins" in text or "codex_core_skills" in text:
        return ""
    return text


def _truncate_log_line(message: str, limit: int = 500) -> str:
    if len(message) <= limit:
        return message
    return message[: limit - 15] + "... [truncated]"
