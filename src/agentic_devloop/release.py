from __future__ import annotations

import json
import shlex
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Literal

from pydantic import Field, field_validator, model_validator

from agentic_devloop.artifacts import cleanup_task_artifacts
from agentic_devloop.budget import build_budget_ledger, build_tuning_report
from agentic_devloop.config import load_project_config
from agentic_devloop.evidence import (
    write_feature_review_decision,
    write_feature_review_recheck,
    write_release_soft_gate_decisions,
)
from agentic_devloop.feature_review import (
    FeatureReviewContextError,
    assemble_feature_review_context,
    generate_repair_contracts_for_required_findings,
    invoke_feature_reviewer,
    render_feature_review_prompt,
)
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
    FeatureReviewDecision,
    FeatureReviewRecommendation,
    FeatureReviewRecheckRecord,
    OverlapFinding,
    ReleaseOverlapReport,
    ReleasePlan,
    ReleaseSoftGateDecisionRecord,
    ReviewDecision,
    SoftGateDecision,
    SoftGateDecisionOutcome,
    SoftGateFinding,
    SoftGateSeverity,
    StrictModel,
    TaskSoftGateDecisionRecord,
    TaskContract,
)
from agentic_devloop.orchestrator import ExecutorProtocol, TaskRunResult, branch_name, run_task
from agentic_devloop.process import run_process
from agentic_devloop.runtime_supervisor import (
    BacklogStateReference,
    BudgetLedgerPaths,
    EvidenceBundlePaths,
    RawLogPaths,
    ReleaseEvent,
    ReleaseEventKind,
    ReleaseSummaryReference,
    RepairDecisionClassification,
    RepairActionKind,
    RuntimeSupervisor,
    RuntimeSupervisorDecisionKind,
    RuntimeSupervisorStopReason,
    RuntimeSupervisorInput,
    TuningReportPaths,
)
from agentic_devloop.state_review import (
    collect_state_review_snapshot,
    write_state_review_snapshot_artifact,
)
from agentic_devloop.yaml_io import load_yaml_model


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
    finalization_gate: dict[str, object] | None = None


@dataclass(frozen=True)
class FeatureReviewLoopResult:
    task_results: list[TaskRunResult]
    feature_review_path: Path | None
    feature_review_recheck_path: Path | None
    feature_review_decision: FeatureReviewDecision | None
    feature_review_recheck: FeatureReviewRecheckRecord | None
    gating_decision: Decision


_RELEASE_BUDGET_SOFT_OVERAGE_RATIO = 0.2
_FEATURE_REVIEW_MAX_REPAIR_LOOPS = 2


class ReleaseFinalizationGate(StrictModel):
    allowed: bool
    reason: Literal[
        "allowed",
        "unresolved_required_findings",
        "release_decision_not_accepted",
    ]
    unresolved_required_finding_ids: list[str] = Field(default_factory=list)
    decision: Decision

    @field_validator("unresolved_required_finding_ids")
    @classmethod
    def unresolved_required_finding_ids_must_be_sorted_unique_and_non_empty(
        cls, values: list[str]
    ) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            if not value.strip():
                raise ValueError("unresolved_required_finding_ids must not contain empty strings")
            cleaned.append(value)
        if cleaned != sorted(cleaned):
            raise ValueError("unresolved_required_finding_ids must be sorted")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("unresolved_required_finding_ids must not contain duplicates")
        return cleaned

    @model_validator(mode="after")
    def gate_invariants(self) -> "ReleaseFinalizationGate":
        if self.allowed:
            if self.reason != "allowed":
                raise ValueError("finalization gate allowed=true requires reason='allowed'")
            if self.unresolved_required_finding_ids:
                raise ValueError("finalization gate allowed=true requires no unresolved required finding ids")
            if self.decision != Decision.ACCEPTED:
                raise ValueError("finalization gate allowed=true requires decision='accepted'")
            return self

        if self.reason == "unresolved_required_findings" and not self.unresolved_required_finding_ids:
            raise ValueError(
                "finalization gate reason='unresolved_required_findings' requires unresolved_required_finding_ids"
            )
        if self.reason == "release_decision_not_accepted" and self.decision == Decision.ACCEPTED:
            raise ValueError(
                "finalization gate reason='release_decision_not_accepted' requires decision != 'accepted'"
            )
        return self


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
    feature_review_source_tasks = _feature_review_source_contracts(
        release_id=release_id,
        contracts_dir=contracts_dir,
        selected_tasks=selected_tasks,
    )
    _ensure_no_existing_task_branches(config.repo_path, release_id, selected_tasks)
    overlap_report = analyze_contract_overlaps(
        selected_tasks,
        unsafe_overlap_paths=config.unsafe_overlap_paths,
    )
    if overlap_report.has_blocking_findings:
        details = "; ".join(
            f"{finding.first_task_id}/{finding.second_task_id}: {finding.pattern}"
            for finding in overlap_report.findings
            if finding.severity == "blocking"
        )
        raise ValueError(f"release contracts are unsafe for {execution_mode} execution: {details}")

    _report(progress, f"event=release_started run_id={run_id} release={release_id} tasks={len(selected_contracts)} mode={execution_mode}")
    _report(progress, f"event=release_logs log={log_path} raw_log={raw_log_path}")
    feature_branch = integration_branch or feature_branch_name(release_id)
    ensure_branch_from_base(config.repo_path, feature_branch, config.default_base_branch)
    _report(progress, f"event=integration_branch branch={feature_branch} base={config.default_base_branch}")
    if overlap_report.findings:
        _report(progress, f"event=overlap_findings count={len(overlap_report.findings)}")

    completed_task_ids = _completed_release_task_ids(
        runs_dir=runs_dir,
        release_id=release_id,
        integration_branch=feature_branch,
    )
    task_inputs: list[tuple[Path, TaskContract]] = []
    skipped_completed_task_ids: list[str] = []
    for contract_path, task in zip(selected_contracts, selected_tasks):
        if task.task_id in completed_task_ids:
            skipped_completed_task_ids.append(task.task_id)
            continue
        task_inputs.append((contract_path, task))
    dependencies = _release_dependency_map(
        [task for _, task in task_inputs],
        overlap_report,
        completed_task_ids=completed_task_ids,
    )
    if completed_task_ids:
        _report(
            progress,
            "event=completed_release_dependencies tasks="
            + json.dumps(sorted(completed_task_ids), sort_keys=True),
        )
    if skipped_completed_task_ids:
        _report(
            progress,
            "event=completed_release_tasks_skipped tasks="
            + json.dumps(sorted(skipped_completed_task_ids), sort_keys=True),
        )
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
            completed_task_ids=completed_task_ids,
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
            release_run_dir=release_root,
            release_id=release_id,
            run_id=run_id,
            repo_state_path=config.repo_state_path,
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

    feature_review_path: Path | None = None
    feature_review_recheck_path: Path | None = None
    feature_review_decision: FeatureReviewDecision | None = None
    feature_review_recheck: FeatureReviewRecheckRecord | None = None

    task_decision = (
        Decision.ACCEPTED
        if not task_results and skipped_completed_task_ids
        else _release_decision([result.decision for result in task_results])
    )
    if task_decision == Decision.ACCEPTED and "reviewer" in config.model_roles:
        feature_review_loop = _run_feature_review_and_repair_loop(
            project_id=project_id,
            config=config,
            release_id=release_id,
            run_id=run_id,
            release_root=release_root,
            runs_dir=runs_dir,
            config_dir=config_dir,
            integration_branch=feature_branch,
            task_results=task_results,
            source_contracts=feature_review_source_tasks,
            executor=executor,
            verification_timeout_seconds=verification_timeout_seconds,
            allow_dirty=allow_dirty,
            commit_on_accept=commit_on_accept,
            merge_on_accept=merge_on_accept,
            push_on_accept=push_on_accept,
            debug_keep_artifacts=debug_keep_artifacts,
            progress=progress,
        )
        task_results = feature_review_loop.task_results
        feature_review_path = feature_review_loop.feature_review_path
        feature_review_recheck_path = feature_review_loop.feature_review_recheck_path
        feature_review_decision = feature_review_loop.feature_review_decision
        feature_review_recheck = feature_review_loop.feature_review_recheck
        task_decision = _release_decision([result.decision for result in task_results])
        if feature_review_loop.gating_decision != Decision.ACCEPTED:
            task_decision = feature_review_loop.gating_decision
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
    budget_evaluation = _evaluate_release_budget(budget_ledger)
    budget_violations = budget_evaluation["severe_violations"]
    soft_budget_findings = budget_evaluation["soft_findings"]
    release_metrics["budget_violations"] = budget_violations
    release_metrics["soft_budget_findings"] = soft_budget_findings
    release_soft_gate_decision_path: Path | None = None
    decision = _release_decision_with_budget(
        task_decision,
        severe_budget_violations=budget_violations,
    )
    if task_decision == Decision.ACCEPTED and soft_budget_findings:
        release_soft_gate_decision_path = _write_release_budget_soft_decision(
            runs_dir=runs_dir,
            run_id=run_id,
            release_id=release_id,
            findings=soft_budget_findings,
            progress=progress,
        )
    metrics_path = _write_release_metrics(runs_dir=runs_dir, run_id=run_id, metrics=release_metrics)
    if decision != task_decision:
        release_metrics["decision"] = decision
        metrics_path = _write_release_metrics(runs_dir=runs_dir, run_id=run_id, metrics=release_metrics)
        _report(progress, "event=release_budget_exceeded violations=" + json.dumps(budget_violations, sort_keys=True))
    finalization_gate = _build_release_finalization_gate(
        decision=decision,
        feature_review_decision=feature_review_decision,
        feature_review_recheck=feature_review_recheck,
    )
    if not bool(finalization_gate["allowed"]):
        _report(
            progress,
            "event=release_finalization_blocked reason="
            + str(finalization_gate["reason"])
            + " unresolved_required_findings="
            + json.dumps(finalization_gate["unresolved_required_finding_ids"], sort_keys=True),
        )
    finalization = _finalize_release(
        repo_path=config.repo_path,
        integration_branch=feature_branch,
        base_branch=config.default_base_branch,
        decision=decision,
        allowed=bool(finalization_gate["allowed"]),
        blocked_reason=str(finalization_gate["reason"]),
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
        integration_commit=_git_rev_parse(config.repo_path, feature_branch),
        finalization=finalization,
        budget_path=budget_path,
        tuning_path=tuning_path,
        budget_violations=budget_violations,
        soft_budget_findings=soft_budget_findings,
        release_soft_gate_decision_path=release_soft_gate_decision_path,
        feature_review_path=feature_review_path,
        feature_review_recheck_path=feature_review_recheck_path,
        finalization_gate=finalization_gate,
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
        soft_budget_findings=soft_budget_findings,
        release_soft_gate_decision_path=release_soft_gate_decision_path,
        feature_review_decision=feature_review_decision,
        feature_review_path=feature_review_path,
        feature_review_recheck=feature_review_recheck,
        feature_review_recheck_path=feature_review_recheck_path,
        finalization_gate=finalization_gate,
    )
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
        finalization_gate=finalization_gate,
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


def _feature_review_source_contracts(
    *,
    release_id: str,
    contracts_dir: Path,
    selected_tasks: list[TaskContract],
) -> list[TaskContract]:
    """Feature review sees the whole feature diff, including prior release slices."""
    by_task_id = {task.task_id: task for task in selected_tasks}
    for path in sorted(contracts_dir.glob("*.yaml")):
        try:
            task = load_yaml_model(path, TaskContract)
        except Exception:
            continue
        if task.release_id == release_id:
            by_task_id.setdefault(task.task_id, task)
    return [by_task_id[task_id] for task_id in sorted(by_task_id)]


def collect_release_planning_state_review_snapshot(
    *,
    config_repo_path: Path,
    repo_state_path: Path | None,
    runs_dir: Path,
    planning_artifacts_dir: Path,
    now: datetime | None = None,
) -> Path:
    if not planning_artifacts_dir.exists():
        raise ValueError(
            f"release planning artifacts directory does not exist: {planning_artifacts_dir}"
        )
    snapshot = collect_state_review_snapshot(
        repo_path=config_repo_path,
        repo_state_path=repo_state_path,
        runs_dir=runs_dir,
        now=now,
    )
    return write_state_review_snapshot_artifact(
        snapshot=snapshot,
        artifacts_dir=planning_artifacts_dir,
    )


def _run_release_sequential(
    *,
    project_id: str,
    config_repo_path: Path,
    config_dir: Path,
    runs_dir: Path,
    release_run_dir: Path,
    release_id: str,
    run_id: str,
    repo_state_path: Path | None,
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
    supervisor_dir = release_run_dir / "runtime_supervisor"
    supervisor_dir.mkdir(parents=True, exist_ok=True)
    supervisor_log_path = supervisor_dir / "supervisor.log"
    supervisor = RuntimeSupervisor()
    supervisor_max_retries = max(0, min(3, load_project_config(project_id, config_dir, validate_repo=True).budget.max_executor_attempts_per_task))
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

        if result.decision.decision != Decision.ACCEPTED:
            result = _attempt_runtime_supervisor_repair_and_resume(
                project_id=project_id,
                config_repo_path=config_repo_path,
                config_dir=config_dir,
                runs_dir=runs_dir,
                release_run_dir=release_run_dir,
                release_id=release_id,
                release_run_id=run_id,
                repo_state_path=repo_state_path,
                task_base_branch=task_base_branch,
                contract_path=contract_path,
                task=task,
                initial_result=result,
                supervisor=supervisor,
                supervisor_log_path=supervisor_log_path,
                max_retries=supervisor_max_retries,
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


def _attempt_runtime_supervisor_repair_and_resume(
    *,
    project_id: str,
    config_repo_path: Path,
    config_dir: Path,
    runs_dir: Path,
    release_run_dir: Path,
    release_id: str,
    release_run_id: str,
    repo_state_path: Path | None,
    task_base_branch: str,
    contract_path: Path,
    task: TaskContract,
    initial_result: TaskRunResult,
    supervisor: RuntimeSupervisor,
    supervisor_log_path: Path,
    max_retries: int,
    executor: ExecutorProtocol | None,
    verification_timeout_seconds: int,
    allow_dirty: bool,
    commit_on_accept: bool,
    merge_on_accept: bool,
    push_on_accept: bool,
    debug_keep_artifacts: bool,
    progress: Callable[[str], None] | None,
) -> TaskRunResult:
    supervisor_dir = release_run_dir / "runtime_supervisor"
    supervisor_dir.mkdir(parents=True, exist_ok=True)
    temp_evidence_dir = supervisor_dir / "temp"
    temp_evidence_dir.mkdir(parents=True, exist_ok=True)

    retry_budget_path = supervisor_dir / "retry_budget_ledger.json"
    repair_budget_path = supervisor_dir / "repair_budget_ledger.json"
    model_tuning_path = supervisor_dir / "model_tuning_report.json"
    verification_tuning_path = supervisor_dir / "verification_tuning_report.json"
    for path in (retry_budget_path, repair_budget_path, model_tuning_path, verification_tuning_path):
        if not path.exists():
            path.write_text("[]\n" if path.name.endswith("ledger.json") else "{}\n", encoding="utf-8")

    backlog_state_reference = _runtime_supervisor_backlog_state_reference(
        config_repo_path=config_repo_path,
        repo_state_path=repo_state_path,
        supervisor_dir=supervisor_dir,
        active_epic_id=release_id,
    )

    repair_evidence_path = supervisor_dir / f"repair_{task.task_id}.json"
    repair_evidence: dict[str, object] = {
        "release_id": release_id,
        "release_run_id": release_run_id,
        "task_id": task.task_id,
        "contract_path": str(contract_path),
        "initial_result": _task_result_reference(initial_result),
        "attempts": [],
        "final_result": None,
    }

    current_result = initial_result
    attempt = 1
    while attempt <= max_retries:
        classification, event_kind, failure_category = _runtime_supervisor_classification_for_task_result(
            result=current_result,
            task=task,
        )
        if classification is None:
            break

        _report(
            progress,
            f"event=repair_attempt_started task={task.task_id} attempt={attempt} classification={classification}",
        )
        _append_supervisor_log(
            supervisor_log_path,
            f"event=repair_attempt_started task={task.task_id} attempt={attempt} classification={classification}\n",
        )

        release_event = _runtime_supervisor_write_release_event(
            supervisor_dir=supervisor_dir,
            task_id=task.task_id,
            attempt=attempt,
            kind=event_kind,
            message=f"task={task.task_id} decision={current_result.decision.decision} category={failure_category}",
        )
        release_summary_ref = _runtime_supervisor_write_release_state_summary(
            supervisor_dir=supervisor_dir,
            release_id=release_id,
            release_run_id=release_run_id,
            task=task,
            attempt=attempt,
            result=current_result,
        )
        evidence_paths = _runtime_supervisor_evidence_paths(current_result)
        raw_log_paths = RawLogPaths(
            supervisor_log_path=supervisor_log_path,
            worker_stdout_path=evidence_paths.bundle_path / "executor_stdout.log",
            worker_stderr_path=evidence_paths.bundle_path / "executor_stderr.log",
        )
        budgets = BudgetLedgerPaths(
            repair_budget_ledger_path=repair_budget_path,
            retry_budget_ledger_path=retry_budget_path,
        )
        tuning = TuningReportPaths(
            model_tuning_report_path=model_tuning_path,
            verification_tuning_report_path=verification_tuning_path,
        )
        supervisor_input = RuntimeSupervisorInput(
            classification=classification,
            attempt=attempt,
            max_retries=max_retries,
            release_event=release_event,
            release_summary=release_summary_ref,
            evidence_bundle_paths=EvidenceBundlePaths(
                bundle_path=evidence_paths.bundle_path,
                changed_files_path=evidence_paths.changed_files_path,
                verification_log_path=evidence_paths.verification_log_path,
            ),
            raw_log_paths=raw_log_paths,
            budget_ledger_paths=budgets,
            tuning_report_paths=tuning,
            backlog_state_reference=backlog_state_reference,
        )
        decision = supervisor.decide(supervisor_input)
        _report(
            progress,
            f"event=repair_decision task={task.task_id} attempt={attempt} decision={decision.decision} action={decision.action.action_kind if decision.action else None}",
        )
        repair_attempt_record: dict[str, object] = {
            "attempt": attempt,
            "classification": str(decision.classification),
            "decision": str(decision.decision),
            "retryable": bool(decision.retryable),
            "reason": decision.reason,
            "remaining_retries": decision.remaining_retries,
            "action_kind": str(decision.action.action_kind) if decision.action else None,
            "result": _task_result_reference(current_result),
        }

        if decision.decision != RuntimeSupervisorDecisionKind.RETRY or decision.action is None:
            repair_attempt_record["stop_reason"] = str(decision.stop_reason) if decision.stop_reason else None
            repair_evidence["attempts"].append(repair_attempt_record)
            _runtime_supervisor_write_repair_evidence(repair_evidence_path, repair_evidence)
            _report(progress, f"event=repair_stopped task={task.task_id} reason={json.dumps(decision.reason)}")
            return current_result

        if decision.action.action_kind != RepairActionKind.RELEASE_RESUME:
            repair_attempt_record["unsupported_action"] = str(decision.action.action_kind)
            repair_evidence["attempts"].append(repair_attempt_record)
            _runtime_supervisor_write_repair_evidence(repair_evidence_path, repair_evidence)
            _report(
                progress,
                f"event=repair_unsupported task={task.task_id} action={decision.action.action_kind}",
            )
            return current_result

        # Validate release resume intent via supervisor applier.
        resume_action_id = f"release_resume/{task.task_id}/attempt-{attempt}"
        resume_budget = max(0, max_retries - attempt)
        applier_result = supervisor.apply_release_resume_intent(
            source_evidence_paths=decision.action.source_evidence_paths,
            action_id=resume_action_id,
            retry_budget=resume_budget,
            stop_reason_fallback=RuntimeSupervisorStopReason.EXHAUSTED_RETRY_BUDGET,
        )
        repair_attempt_record["applier_applied"] = bool(applier_result.applied)
        repair_attempt_record["applier_stop_evidence"] = (
            {
                "action_kind": str(applier_result.stop_evidence.action_kind),
                "kind": str(applier_result.stop_evidence.kind),
                "reason": applier_result.stop_evidence.reason,
            }
            if applier_result.stop_evidence
            else None
        )
        if not applier_result.applied or applier_result.proposal is None:
            repair_evidence["attempts"].append(repair_attempt_record)
            _runtime_supervisor_write_repair_evidence(repair_evidence_path, repair_evidence)
            _report(progress, f"event=repair_resume_blocked task={task.task_id}")
            return current_result

        # Stop if the task already violated its allowed_files scope.
        changed_files = _runtime_supervisor_read_changed_files(evidence_paths.changed_files_path)
        if any(not _path_allowed(path, task.allowed_files) for path in changed_files):
            repair_attempt_record["blocked_reason"] = "contract_boundary_violation"
            repair_attempt_record["changed_files"] = changed_files
            repair_evidence["attempts"].append(repair_attempt_record)
            repair_evidence["final_result"] = _task_result_reference(current_result)
            _runtime_supervisor_write_repair_evidence(repair_evidence_path, repair_evidence)
            _report(progress, f"event=repair_boundary_violation task={task.task_id}")
            return current_result

        repair_evidence["attempts"].append(repair_attempt_record)
        _runtime_supervisor_write_repair_evidence(repair_evidence_path, repair_evidence)
        _report(progress, f"event=task_resumed task={task.task_id} attempt={attempt}")

        # Resume by rerunning the same task contract with full verification/review.
        current_result = _run_one_release_task(
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
        if current_result.decision.decision == Decision.ACCEPTED:
            repair_evidence["final_result"] = _task_result_reference(current_result)
            _runtime_supervisor_write_repair_evidence(repair_evidence_path, repair_evidence)
            _report(progress, f"event=repair_succeeded task={task.task_id} attempt={attempt}")
            return current_result

        attempt += 1

    # Retry budget exhausted or unsupported.
    repair_evidence["final_result"] = _task_result_reference(current_result)
    _runtime_supervisor_write_repair_evidence(repair_evidence_path, repair_evidence)
    _report(progress, f"event=repair_exhausted task={task.task_id} attempts={max_retries}")
    return current_result


def _append_supervisor_log(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _runtime_supervisor_write_repair_evidence(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _task_result_reference(result: TaskRunResult) -> dict[str, object]:
    return {
        "run_id": result.run_id,
        "bundle_path": str(result.bundle_path),
        "decision": str(result.decision.decision),
        "rationale": result.decision.rationale,
    }


def _runtime_supervisor_evidence_paths(result: TaskRunResult) -> EvidenceBundlePaths:
    bundle_path = result.bundle_path
    return EvidenceBundlePaths(
        bundle_path=bundle_path,
        changed_files_path=bundle_path / "changed_files.txt",
        verification_log_path=bundle_path / "verification.log",
    )


def _runtime_supervisor_read_changed_files(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _runtime_supervisor_backlog_state_reference(
    *,
    config_repo_path: Path,
    repo_state_path: Path | None,
    supervisor_dir: Path,
    active_epic_id: str,
) -> BacklogStateReference:
    if repo_state_path is None:
        placeholder = supervisor_dir / "backlog_state_missing.txt"
        placeholder.write_text("repo_state_path is not configured for this project.\n", encoding="utf-8")
        return BacklogStateReference(backlog_state_path=placeholder, active_epic_id=active_epic_id)
    root = repo_state_path if repo_state_path.is_absolute() else config_repo_path / repo_state_path
    backlog_path = root / "backlog_state.yaml"
    if backlog_path.exists():
        return BacklogStateReference(backlog_state_path=backlog_path, active_epic_id=active_epic_id)
    placeholder = supervisor_dir / "backlog_state_missing.txt"
    placeholder.write_text(f"Missing backlog_state.yaml at {backlog_path}.\n", encoding="utf-8")
    return BacklogStateReference(backlog_state_path=placeholder, active_epic_id=active_epic_id)


def _runtime_supervisor_write_release_event(
    *,
    supervisor_dir: Path,
    task_id: str,
    attempt: int,
    kind: ReleaseEventKind,
    message: str,
) -> ReleaseEvent:
    event_dir = supervisor_dir / "events"
    event_dir.mkdir(parents=True, exist_ok=True)
    event_path = event_dir / f"{task_id}_attempt_{attempt}.json"
    event_payload = {
        "kind": str(kind),
        "task_id": task_id,
        "attempt": attempt,
        "message": message,
    }
    event_path.write_text(json.dumps(event_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return ReleaseEvent(kind=kind, message=message, event_path=event_path)


def _runtime_supervisor_write_release_state_summary(
    *,
    supervisor_dir: Path,
    release_id: str,
    release_run_id: str,
    task: TaskContract,
    attempt: int,
    result: TaskRunResult,
) -> ReleaseSummaryReference:
    summary_path = supervisor_dir / "release_state.json"
    payload = {
        "release_id": release_id,
        "release_run_id": release_run_id,
        "task_id": task.task_id,
        "attempt": attempt,
        "decision": str(result.decision.decision),
        "bundle_path": str(result.bundle_path),
    }
    summary_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return ReleaseSummaryReference(release_id=release_id, summary_path=summary_path)


def _runtime_supervisor_classification_for_task_result(
    *,
    result: TaskRunResult,
    task: TaskContract,
) -> tuple[RepairDecisionClassification | None, ReleaseEventKind, str]:
    category = "unknown"
    diagnosis_path = result.bundle_path / "failure_diagnosis.yaml"
    if diagnosis_path.exists():
        try:
            import yaml

            diagnosis = yaml.safe_load(diagnosis_path.read_text(encoding="utf-8")) or {}
            category = str(diagnosis.get("category") or category)
        except Exception:
            category = "unknown"

    if result.decision.decision == Decision.ESCALATED:
        return (RepairDecisionClassification.UNSAFE_POLICY_EXPANSION, ReleaseEventKind.RELEASE_BLOCKED, category)
    if category == "contract_mismatch":
        return (RepairDecisionClassification.CONTRACT_BOUNDARY_VIOLATION, ReleaseEventKind.RELEASE_BLOCKED, category)
    if category == "verification_failure":
        return (RepairDecisionClassification.RELEASE_RESUMABLE, ReleaseEventKind.VERIFICATION_FAILED, category)
    if category == "timeout":
        return (RepairDecisionClassification.TASK_SCOPE_OVERBROAD, ReleaseEventKind.TASK_FAILED, category)
    if category == "model_quota":
        return (RepairDecisionClassification.MISSING_CREDENTIALS, ReleaseEventKind.RELEASE_BLOCKED, category)
    if category == "executor_error":
        return (RepairDecisionClassification.RELEASE_RESUMABLE, ReleaseEventKind.TASK_FAILED, category)

    # Fallback: retry only for needs_revision/failed, otherwise stop.
    if result.decision.decision in {Decision.NEEDS_REVISION, Decision.FAILED}:
        return (RepairDecisionClassification.RELEASE_RESUMABLE, ReleaseEventKind.TASK_FAILED, category)
    return (None, ReleaseEventKind.TASK_FAILED, category)


def _path_allowed(path: str, allowed_patterns: list[str]) -> bool:
    normalized = path.lstrip("./")
    return any(fnmatch(normalized, pattern) for pattern in allowed_patterns)


def _run_release_parallel(
    *,
    project_id: str,
    config_repo_path: Path,
    config_dir: Path,
    runs_dir: Path,
    task_base_branch: str,
    task_inputs: list[tuple[Path, TaskContract]],
    dependencies: dict[str, list[str]],
    completed_task_ids: set[str],
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
    completed: set[str] = set(completed_task_ids)
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


def _run_feature_review_and_repair_loop(
    *,
    project_id: str,
    config: "ProjectConfig",
    release_id: str,
    run_id: str,
    release_root: Path,
    runs_dir: Path,
    config_dir: Path,
    integration_branch: str,
    task_results: list[TaskRunResult],
    source_contracts: list[TaskContract],
    executor: ExecutorProtocol | None,
    verification_timeout_seconds: int,
    allow_dirty: bool,
    commit_on_accept: bool,
    merge_on_accept: bool,
    push_on_accept: bool,
    debug_keep_artifacts: bool,
    progress: Callable[[str], None] | None,
) -> FeatureReviewLoopResult:
    output_root = release_root / "feature_review"
    output_root.mkdir(parents=True, exist_ok=True)
    reviewer_config = config.model_roles.get("reviewer", config.executor)

    gating_decision = Decision.ACCEPTED
    all_task_results: list[TaskRunResult] = []
    feature_review_path: Path | None = None
    feature_review_recheck_path: Path | None = None
    feature_review_decision: FeatureReviewDecision | None = None
    feature_review_recheck: FeatureReviewRecheckRecord | None = None
    outstanding_required_finding_ids: set[str] = set()

    def allowed_verification_commands() -> list[str]:
        commands: list[str] = []
        for profile in config.verification_profiles.values():
            commands.extend(profile.commands)
        results = all_task_results or task_results
        for result in results:
            run_state = _read_json_object(result.bundle_path / "run_state.json")
            verification_results = run_state.get("verification_results", [])
            if not isinstance(verification_results, list):
                continue
            for item in verification_results:
                if not isinstance(item, dict):
                    continue
                command = item.get("command")
                if isinstance(command, str) and command.strip():
                    commands.append(command.strip())
        seen: set[str] = set()
        ordered: list[str] = []
        for command in commands:
            normalized = str(command).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered

    def run_review(attempt: int) -> FeatureReviewDecision:
        nonlocal feature_review_path
        nonlocal feature_review_decision
        attempt_dir = output_root / f"attempt_{attempt:02d}"
        _report(progress, f"event=feature_review_started attempt={attempt} output_dir={attempt_dir}")
        branches_base = config.default_base_branch
        try:
            context = assemble_feature_review_context(
                repo_path=config.repo_path,
                release_id=release_id,
                base_branch=branches_base,
                integration_branch=integration_branch,
                runs_dir=runs_dir,
            )
            prompt = render_feature_review_prompt(
                context=context,
                repo_path=config.repo_path,
                runs_dir=runs_dir,
            )
            backend = invoke_feature_reviewer(
                config=reviewer_config,
                repo_path=config.repo_path,
                prompt=prompt,
                release_id=release_id,
                output_dir=attempt_dir,
            )
            decision = backend.decision
        except FeatureReviewContextError as error:
            decision = FeatureReviewDecision.model_validate(
                {
                    "release_id": release_id,
                    "reviewer": "deterministic",
                    "summary": f"Feature review context failure: {error}",
                    "recommendation": "escalate",
                    "accepted_risks": [],
                    "rerun_verification_commands": [],
                    "findings": [],
                }
            )
        feature_review_decision = decision
        feature_review_path = write_feature_review_decision(release_root, decision)
        _report(progress, f"event=feature_review_completed attempt={attempt} recommendation={decision.recommendation.value}")
        return decision

    def rerun_verification(attempt: int, decision: FeatureReviewDecision) -> bool:
        rerun_dir = output_root / f"verification_rerun_{attempt:02d}"
        rerun_dir.mkdir(parents=True, exist_ok=True)
        commands = list(config.verification_profiles["default"].commands)
        allowed = set(allowed_verification_commands())
        if decision.rerun_verification_commands:
            unknown = [cmd for cmd in decision.rerun_verification_commands if cmd not in allowed]
            if unknown:
                _report(
                    progress,
                    "event=feature_review_verification_commands_ignored commands="
                    + json.dumps(unknown, sort_keys=True),
                )
            requested = [cmd for cmd in decision.rerun_verification_commands if cmd in allowed]
            if requested:
                commands = requested
        return _run_integration_verification_rerun(
            repo_path=config.repo_path,
            integration_branch=integration_branch,
            worktree_path=rerun_dir / "worktree",
            commands=commands,
            timeout_seconds=verification_timeout_seconds,
            log_path=rerun_dir / "verification.log",
            progress=progress,
        )

    all_task_results = list(task_results)
    decision = run_review(attempt=1)

    for loop_index in range(_FEATURE_REVIEW_MAX_REPAIR_LOOPS + 1):
        required_findings = [finding for finding in decision.findings if finding.required_repairs]
        if required_findings:
            outstanding_required_finding_ids.update(finding.finding_id for finding in required_findings)
        optional_findings = [
            finding
            for finding in decision.findings
            if not finding.required_repairs and finding.optional_follow_ups
        ]

        if decision.recommendation == FeatureReviewRecommendation.ESCALATE:
            gating_decision = Decision.ESCALATED
            unresolved_finding_ids = [finding.finding_id for finding in decision.findings]
            if not unresolved_finding_ids and outstanding_required_finding_ids:
                unresolved_finding_ids = sorted(outstanding_required_finding_ids)
            feature_review_recheck = FeatureReviewRecheckRecord(
                release_id=release_id,
                unresolved_finding_ids=unresolved_finding_ids or [f"{release_id}:feature_review_blocked"],
                resolved_finding_ids=[],
                accepted_finding_ids=[],
                stop_reason="blocked_by_hard_gate",
            )
            feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
            return FeatureReviewLoopResult(
                task_results=all_task_results,
                feature_review_path=feature_review_path,
                feature_review_recheck_path=feature_review_recheck_path,
                feature_review_decision=feature_review_decision,
                feature_review_recheck=feature_review_recheck,
                gating_decision=gating_decision,
            )

        if not required_findings:
            stop_reason = "resolved" if not decision.findings else "accepted_with_rationale"
            optional_finding_ids = {finding.finding_id for finding in optional_findings}
            if stop_reason == "accepted_with_rationale" and not decision.accepted_risks:
                decision = decision.model_copy(
                    update={
                        "accepted_risks": [
                            (
                                "Accepted optional findings after reviewer re-check with no required repairs; "
                                "follow-up remains non-blocking for release finalization."
                            )
                        ]
                    }
                )
                feature_review_decision = decision
                feature_review_path = write_feature_review_decision(release_root, decision)
            feature_review_recheck = FeatureReviewRecheckRecord(
                release_id=release_id,
                unresolved_finding_ids=[],
                resolved_finding_ids=[
                    finding.finding_id
                    for finding in decision.findings
                    if finding.finding_id not in optional_finding_ids
                ],
                accepted_finding_ids=sorted(optional_finding_ids),
                stop_reason=stop_reason,
            )
            feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
            return FeatureReviewLoopResult(
                task_results=all_task_results,
                feature_review_path=feature_review_path,
                feature_review_recheck_path=feature_review_recheck_path,
                feature_review_decision=feature_review_decision,
                feature_review_recheck=feature_review_recheck,
                gating_decision=gating_decision,
            )

        if loop_index >= _FEATURE_REVIEW_MAX_REPAIR_LOOPS:
            gating_decision = Decision.NEEDS_REVISION
            feature_review_recheck = FeatureReviewRecheckRecord(
                release_id=release_id,
                unresolved_finding_ids=[finding.finding_id for finding in required_findings],
                resolved_finding_ids=[],
                accepted_finding_ids=[finding.finding_id for finding in optional_findings],
                stop_reason="blocked_by_retry_budget",
            )
            feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
            return FeatureReviewLoopResult(
                task_results=all_task_results,
                feature_review_path=feature_review_path,
                feature_review_recheck_path=feature_review_recheck_path,
                feature_review_decision=feature_review_decision,
                feature_review_recheck=feature_review_recheck,
                gating_decision=gating_decision,
            )

        generated = generate_repair_contracts_for_required_findings(
            decision=decision,
            source_contracts=source_contracts,
        )
        if not generated:
            gating_decision = Decision.NEEDS_REVISION
            feature_review_recheck = FeatureReviewRecheckRecord(
                release_id=release_id,
                unresolved_finding_ids=[finding.finding_id for finding in required_findings],
                resolved_finding_ids=[],
                accepted_finding_ids=[finding.finding_id for finding in optional_findings],
                stop_reason="blocked_by_hard_gate",
            )
            feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
            return FeatureReviewLoopResult(
                task_results=all_task_results,
                feature_review_path=feature_review_path,
                feature_review_recheck_path=feature_review_recheck_path,
                feature_review_decision=feature_review_decision,
                feature_review_recheck=feature_review_recheck,
                gating_decision=gating_decision,
            )

        repair_dir = output_root / f"repairs_{loop_index + 1:02d}"
        repair_dir.mkdir(parents=True, exist_ok=True)
        _report(progress, f"event=feature_review_repairs_started attempt={loop_index + 1} repairs={len(generated)}")
        for contract in generated:
            contract_path = repair_dir / f"{contract.task_id}.yaml"
            _write_contract_yaml(contract_path, contract.suggested_contract)
            result = _run_one_release_task(
                project_id=project_id,
                config_repo_path=config.repo_path,
                config_dir=config_dir,
                runs_dir=runs_dir,
                task_base_branch=integration_branch,
                contract_path=contract_path,
                task=contract.suggested_contract,
                executor=executor,
                verification_timeout_seconds=verification_timeout_seconds,
                allow_dirty=allow_dirty,
                commit_on_accept=commit_on_accept,
                merge_on_accept=merge_on_accept,
                push_on_accept=push_on_accept,
                debug_keep_artifacts=debug_keep_artifacts,
                progress=progress,
            )
            all_task_results.append(result)
            if result.decision.decision != Decision.ACCEPTED:
                gating_decision = Decision.NEEDS_REVISION
                feature_review_recheck = FeatureReviewRecheckRecord(
                    release_id=release_id,
                    unresolved_finding_ids=[finding.finding_id for finding in required_findings],
                    resolved_finding_ids=[],
                    accepted_finding_ids=[finding.finding_id for finding in optional_findings],
                    stop_reason="blocked_by_hard_gate",
                )
                feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
                return FeatureReviewLoopResult(
                    task_results=all_task_results,
                    feature_review_path=feature_review_path,
                    feature_review_recheck_path=feature_review_recheck_path,
                    feature_review_decision=feature_review_decision,
                    feature_review_recheck=feature_review_recheck,
                    gating_decision=gating_decision,
                )

        verification_ok = rerun_verification(loop_index + 1, decision)
        if not verification_ok:
            gating_decision = Decision.NEEDS_REVISION
            feature_review_recheck = FeatureReviewRecheckRecord(
                release_id=release_id,
                unresolved_finding_ids=[finding.finding_id for finding in required_findings],
                resolved_finding_ids=[],
                accepted_finding_ids=[finding.finding_id for finding in optional_findings],
                stop_reason="blocked_by_hard_gate",
            )
            feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
            return FeatureReviewLoopResult(
                task_results=all_task_results,
                feature_review_path=feature_review_path,
                feature_review_recheck_path=feature_review_recheck_path,
                feature_review_decision=feature_review_decision,
                feature_review_recheck=feature_review_recheck,
                gating_decision=gating_decision,
            )
        decision = run_review(attempt=loop_index + 2)

    gating_decision = Decision.NEEDS_REVISION
    feature_review_recheck = FeatureReviewRecheckRecord(
        release_id=release_id,
        unresolved_finding_ids=[finding.finding_id for finding in decision.findings],
        resolved_finding_ids=[],
        accepted_finding_ids=[],
        stop_reason="blocked_by_retry_budget",
    )
    feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
    return FeatureReviewLoopResult(
        task_results=all_task_results,
        feature_review_path=feature_review_path,
        feature_review_recheck_path=feature_review_recheck_path,
        feature_review_decision=feature_review_decision,
        feature_review_recheck=feature_review_recheck,
        gating_decision=gating_decision,
    )


def _write_contract_yaml(path: Path, contract: TaskContract) -> None:
    import yaml

    path.write_text(yaml.safe_dump(contract.model_dump(mode="json"), sort_keys=False), encoding="utf-8")


def _run_integration_verification_rerun(
    *,
    repo_path: Path,
    integration_branch: str,
    worktree_path: Path,
    commands: list[str],
    timeout_seconds: int,
    log_path: Path,
    progress: Callable[[str], None] | None,
) -> bool:
    repo_path = repo_path.resolve()
    worktree_path = worktree_path.resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []
    cleanup_root = log_path.parent.resolve()
    _assert_safe_feature_review_rerun_worktree(worktree_path, cleanup_root)
    log_lines.append(f"rerun worktree cleanup guard: {worktree_path} is under {cleanup_root}")

    if worktree_path.exists():
        run_process(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_path,
            timeout_seconds=120,
        )

    add = run_process(
        ["git", "worktree", "add", "--detach", str(worktree_path), integration_branch],
        cwd=repo_path,
        timeout_seconds=120,
    )
    log_lines.append(f"$ git worktree add --detach {worktree_path} {integration_branch}")
    log_lines.append(add.stdout.rstrip())
    log_lines.append(add.stderr.rstrip())
    if add.exit_code != 0:
        log_path.write_text("\n".join(line for line in log_lines if line) + "\n", encoding="utf-8")
        _report(progress, f"event=feature_review_verification_worktree_failed error={add.stderr.strip() or add.stdout.strip()}")
        return False

    ok = True
    for command in commands:
        parts = _command_with_env_prefixes(shlex.split(command))
        log_lines.append(f"$ {command}")
        result = run_process(
            parts,
            cwd=worktree_path,
            timeout_seconds=timeout_seconds,
        )
        log_lines.append(result.stdout.rstrip())
        log_lines.append(result.stderr.rstrip())
        if result.exit_code != 0:
            ok = False
            _report(progress, f"event=feature_review_verification_failed command={json.dumps(command)} exit_code={result.exit_code}")
            break

    remove = run_process(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=repo_path,
        timeout_seconds=120,
    )
    log_lines.append(f"$ git worktree remove --force {worktree_path}")
    log_lines.append(remove.stdout.rstrip())
    log_lines.append(remove.stderr.rstrip())
    if remove.exit_code != 0:
        _report(progress, f"event=feature_review_verification_worktree_cleanup_failed error={remove.stderr.strip() or remove.stdout.strip()}")

    log_path.write_text("\n".join(line for line in log_lines if line) + "\n", encoding="utf-8")
    _report(progress, f"event=feature_review_verification_completed ok={str(ok).lower()} log={log_path}")
    return ok


def _assert_safe_feature_review_rerun_worktree(worktree_path: Path, cleanup_root: Path) -> None:
    if worktree_path == cleanup_root or cleanup_root in worktree_path.parents:
        return
    raise ValueError(
        "feature-review verification rerun worktree must be inside the rerun output directory "
        f"before forced cleanup is allowed: worktree={worktree_path} cleanup_root={cleanup_root}"
    )


def _command_with_env_prefixes(parts: list[str]) -> list[str]:
    env_parts: list[str] = []
    command_parts = list(parts)
    while command_parts and _looks_like_env_assignment(command_parts[0]):
        env_parts.append(command_parts.pop(0))
    if not env_parts:
        return parts
    if not command_parts:
        return parts
    return ["/usr/bin/env", *env_parts, *command_parts]


def _looks_like_env_assignment(value: str) -> bool:
    if "=" not in value:
        return False
    name, _value = value.split("=", 1)
    return bool(name) and all(character == "_" or character.isalnum() for character in name)


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
    allowed: bool,
    blocked_reason: str,
    mode: str,
    progress: Callable[[str], None] | None,
) -> FinalizeResult | None:
    if mode == "none":
        return None
    if not allowed:
        _report(progress, f"release_finalization_skipped reason={blocked_reason}")
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
    integration_commit: str,
    finalization: FinalizeResult | None,
    budget_path: Path,
    tuning_path: Path,
    budget_violations: list[str],
    soft_budget_findings: list[str],
    release_soft_gate_decision_path: Path | None,
    feature_review_path: Path | None,
    feature_review_recheck_path: Path | None,
    finalization_gate: dict[str, object],
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
        "soft_budget_findings": soft_budget_findings,
        "release_soft_gate_decision_path": str(release_soft_gate_decision_path) if release_soft_gate_decision_path else None,
        "feature_review_path": str(feature_review_path) if feature_review_path else None,
        "feature_review_recheck_path": str(feature_review_recheck_path) if feature_review_recheck_path else None,
        "finalization_gate": finalization_gate,
        "integration_branch": integration_branch,
        "integration_commit": integration_commit,
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


def _git_rev_parse(repo_path: Path, ref: str) -> str:
    result = run_process(
        ["git", "rev-parse", "--verify", ref],
        cwd=repo_path,
        timeout_seconds=120,
    )
    if result.exit_code != 0:
        raise GitFinalizeError(result.stderr.strip() or result.stdout.strip() or f"git ref not found: {ref}")
    return result.stdout.strip()


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
    soft_budget_findings: list[str],
    release_soft_gate_decision_path: Path | None,
    feature_review_decision: FeatureReviewDecision | None,
    feature_review_path: Path | None,
    feature_review_recheck: FeatureReviewRecheckRecord | None,
    feature_review_recheck_path: Path | None,
    finalization_gate: dict[str, object],
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
    if soft_budget_findings:
        lines.append("- Soft findings accepted with mitigation:")
        lines.extend(f"- Soft finding: {finding}" for finding in soft_budget_findings)
    if release_soft_gate_decision_path is not None:
        lines.append(f"- Soft decision artifact: `{release_soft_gate_decision_path}`")
    if feature_review_path is not None:
        lines.extend(
            [
                "",
                "## Feature Review",
                "",
                f"- Artifact: `{feature_review_path}`",
            ]
        )
        if feature_review_decision is not None:
            lines.append(f"- Recommendation: `{feature_review_decision.recommendation.value}`")
            lines.append(f"- Findings: `{len(feature_review_decision.findings)}`")
        if feature_review_recheck_path is not None:
            lines.append(f"- Recheck artifact: `{feature_review_recheck_path}`")
        if feature_review_recheck is not None and feature_review_recheck.stop_reason is not None:
            lines.append(f"- Recheck status: `{feature_review_recheck.stop_reason}`")
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
                f"- Allowed: `{finalization_gate['allowed']}`",
                f"- Gate reason: `{finalization_gate['reason']}`",
                f"- Unresolved required findings: `{len(finalization_gate['unresolved_required_finding_ids'])}`",
                f"- Merged: `{finalization.merged}`",
                f"- Pushed: `{finalization.pushed}`",
                f"- Error: `{finalization.error or 'none'}`",
                "",
            ]
        )
    elif not bool(finalization_gate["allowed"]):
        lines.extend(
            [
                "## Release Finalization",
                "",
                f"- Allowed: `{finalization_gate['allowed']}`",
                f"- Gate reason: `{finalization_gate['reason']}`",
                f"- Unresolved required findings: `{len(finalization_gate['unresolved_required_finding_ids'])}`",
                "",
            ]
        )
    review_path.write_text("\n".join(lines), encoding="utf-8")
    return review_path


def _build_release_finalization_gate(
    *,
    decision: Decision,
    feature_review_decision: FeatureReviewDecision | None,
    feature_review_recheck: FeatureReviewRecheckRecord | None,
) -> dict[str, object]:
    unresolved_finding_ids = set(feature_review_recheck.unresolved_finding_ids) if feature_review_recheck else set()
    required_finding_ids_from_decision = (
        {
            finding.finding_id
            for finding in feature_review_decision.findings
            if finding.required_repairs
        }
        if feature_review_decision
        else set()
    )
    optional_finding_ids_from_decision = (
        {
            finding.finding_id
            for finding in feature_review_decision.findings
            if not finding.required_repairs and finding.optional_follow_ups
        }
        if feature_review_decision
        else set()
    )
    unresolved_required_finding_ids = sorted(unresolved_finding_ids.intersection(required_finding_ids_from_decision))
    if (
        not unresolved_required_finding_ids
        and unresolved_finding_ids
        and not required_finding_ids_from_decision
        and feature_review_recheck is not None
        and feature_review_recheck.stop_reason in {"blocked_by_retry_budget", "blocked_by_hard_gate"}
    ):
        # Recheck unresolved findings are authoritative when the latest decision no longer
        # carries explicit required findings but the loop still ended in a blocked state.
        unresolved_required_finding_ids = sorted(unresolved_finding_ids.difference(optional_finding_ids_from_decision))
    if unresolved_required_finding_ids:
        gate = ReleaseFinalizationGate(
            allowed=False,
            reason="unresolved_required_findings",
            unresolved_required_finding_ids=unresolved_required_finding_ids,
            decision=decision,
        )
        return gate.model_dump(mode="json")
    if decision != Decision.ACCEPTED:
        gate = ReleaseFinalizationGate(
            allowed=False,
            reason="release_decision_not_accepted",
            unresolved_required_finding_ids=[],
            decision=decision,
        )
        return gate.model_dump(mode="json")
    gate = ReleaseFinalizationGate(
        allowed=True,
        reason="allowed",
        unresolved_required_finding_ids=[],
        decision=decision,
    )
    return gate.model_dump(mode="json")


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


def _evaluate_release_budget(ledger) -> dict[str, list[str]]:
    severe_violations: list[str] = []
    soft_findings: list[str] = []
    for entry in ledger.usage:
        if entry.scope != "release" or entry.over_by is None or entry.over_by <= 0:
            continue
        message = (
            f"{entry.name} exceeded budget: actual {entry.actual} {entry.unit} "
            f"over configured {entry.configured}"
        )
        if _is_soft_release_budget_overage(entry.configured, entry.over_by):
            soft_findings.append(message)
        else:
            severe_violations.append(message)
    return {"severe_violations": severe_violations, "soft_findings": soft_findings}


def _release_decision_with_budget(decision: Decision, severe_budget_violations: list[str]) -> Decision:
    if severe_budget_violations and decision == Decision.ACCEPTED:
        return Decision.FAILED
    return decision


def _is_soft_release_budget_overage(configured: int | float | None, over_by: int | float) -> bool:
    if configured is None or configured <= 0:
        return False
    return (float(over_by) / float(configured)) <= _RELEASE_BUDGET_SOFT_OVERAGE_RATIO


def _write_release_budget_soft_decision(
    *,
    runs_dir: Path,
    run_id: str,
    release_id: str,
    findings: list[str],
    progress: Callable[[str], None] | None,
) -> Path:
    finding_records: list[TaskSoftGateDecisionRecord] = []
    for index, finding in enumerate(findings, start=1):
        finding_id = f"release-budget-{index}"
        finding_records.append(
            TaskSoftGateDecisionRecord(
                task_id="release-budget",
                finding=SoftGateFinding(
                    finding_id=finding_id,
                    severity=SoftGateSeverity.MODERATE,
                    risk=finding,
                    recommended_actions=["Inspect release_budget.json and release_tuning.md before finalizing downstream integrations."],
                    evidence_paths=[runs_dir / run_id / "release_budget.json", runs_dir / run_id / "release_tuning.md"],
                ),
                decision=SoftGateDecision(
                    finding_id=finding_id,
                    decision=SoftGateDecisionOutcome.ACCEPT_WITH_MITIGATION,
                    rationale="Release-level overage remained within the configured soft threshold.",
                    fallback_plan="Split planned follow-up scope or tighten model usage on the next run if this repeats.",
                    validators_rerun=["release_budget.json", "release_metrics.json", "release_tuning.md"],
                    evidence_paths=[runs_dir / run_id / "release_metrics.json"],
                ),
            )
        )
    artifact_path = write_release_soft_gate_decisions(
        runs_dir / run_id,
        ReleaseSoftGateDecisionRecord(release_id=release_id, decisions=finding_records),
    )
    _report(progress, f"event=release_soft_gate_decisions path={artifact_path}")
    return artifact_path


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


def analyze_contract_overlaps(
    tasks: list[TaskContract],
    *,
    unsafe_overlap_paths: list[str] | None = None,
) -> ReleaseOverlapReport:
    findings: list[OverlapFinding] = []
    unsafe_overlap_paths = unsafe_overlap_paths or []
    for index, first in enumerate(tasks):
        for second in tasks[index + 1 :]:
            for first_pattern in first.allowed_files:
                for second_pattern in second.allowed_files:
                    severity = _overlap_severity(
                        first_pattern,
                        second_pattern,
                        unsafe_overlap_paths=unsafe_overlap_paths,
                    )
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


def _completed_release_task_ids(
    *,
    runs_dir: Path,
    release_id: str,
    integration_branch: str,
) -> set[str]:
    completed: set[str] = set()
    for summary_path in sorted(runs_dir.glob(f"*_{release_id}_release/release_summary.json")):
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if summary.get("release_id") != release_id:
            continue
        if summary.get("integration_branch") != integration_branch:
            continue
        for task in summary.get("tasks", []):
            if not isinstance(task, dict):
                continue
            if task.get("decision") != Decision.ACCEPTED:
                continue
            if not task.get("merged"):
                continue
            task_id = task.get("task_id")
            if isinstance(task_id, str) and task_id:
                completed.add(task_id)
    return completed


def _release_dependency_map(
    tasks: list[TaskContract],
    overlap_report: ReleaseOverlapReport,
    *,
    completed_task_ids: set[str] | None = None,
) -> dict[str, list[str]]:
    task_ids = {task.task_id for task in tasks}
    completed_task_ids = completed_task_ids or set()
    dependencies: dict[str, set[str]] = {task.task_id: set(task.depends_on) for task in tasks}
    for task in tasks:
        unknown = sorted(set(task.depends_on) - task_ids - completed_task_ids)
        if unknown:
            raise ValueError(
                f"task {task.task_id} depends on unknown release task(s): {', '.join(unknown)}"
            )
    for finding in overlap_report.findings:
        if finding.severity == "minor":
            if finding.second_task_id not in dependencies:
                continue
            dependencies[finding.second_task_id].add(finding.first_task_id)
    return {
        task_id: sorted(values - completed_task_ids)
        for task_id, values in dependencies.items()
        if values - completed_task_ids
    }


def _overlap_severity(
    first: str,
    second: str,
    *,
    unsafe_overlap_paths: list[str],
) -> str | None:
    if not _patterns_overlap(first, second):
        return None
    if _is_hard_unsafe_overlap(first, second, unsafe_overlap_paths=unsafe_overlap_paths):
        return "blocking"
    return "minor"


def _is_hard_unsafe_overlap(first: str, second: str, *, unsafe_overlap_paths: list[str]) -> bool:
    return any(
        (
            _is_configured_unsafe_overlap(first, second, unsafe_pattern)
            for unsafe_pattern in unsafe_overlap_paths
        )
    ) or any(
        (
            _is_forbidden_overlap_path(first),
            _is_forbidden_overlap_path(second),
            _is_out_of_scope_overlap_path(first),
            _is_out_of_scope_overlap_path(second),
            _is_generated_artifact_path(first),
            _is_generated_artifact_path(second),
            _is_lockfile_path(first),
            _is_lockfile_path(second),
            _is_migration_path(first),
            _is_migration_path(second),
        )
    )


def _is_configured_unsafe_overlap(first: str, second: str, unsafe_pattern: str) -> bool:
    return _patterns_overlap(first, unsafe_pattern) or _patterns_overlap(second, unsafe_pattern)


def _is_forbidden_overlap_path(pattern: str) -> bool:
    normalized = pattern.strip().lstrip("./")
    return normalized == ".git" or normalized.startswith(".git/")


def _is_out_of_scope_overlap_path(pattern: str) -> bool:
    normalized = pattern.strip()
    return normalized in {"*", "**", "**/*", "./**", "./**/*", "/**", "/**/*"}


def _is_generated_artifact_path(pattern: str) -> bool:
    normalized = pattern.strip().lstrip("./").lower()
    if normalized.startswith("dist/") or normalized.startswith("build/") or normalized.startswith("runs/"):
        return True
    if normalized.endswith(".min.js") or normalized.endswith(".generated.py"):
        return True
    return "/generated/" in normalized or normalized.startswith("generated/")


def _is_lockfile_path(pattern: str) -> bool:
    normalized = pattern.strip().lstrip("./")
    lockfile_names = {
        "poetry.lock",
        "uv.lock",
        "pdm.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.lock",
        "Gemfile.lock",
        "composer.lock",
    }
    return Path(normalized).name in lockfile_names


def _is_migration_path(pattern: str) -> bool:
    normalized = pattern.strip().lstrip("./").lower()
    return (
        "/migrations/" in normalized
        or normalized.startswith("migrations/")
        or normalized.endswith("/migrations")
        or normalized.startswith("alembic/versions/")
    )


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
    formatter = _HumanLogFormatter()

    def report(message: str) -> None:
        timestamp = datetime.now(UTC).isoformat()
        raw_line = f"{timestamp} {message}\n"
        display_messages = formatter.format(message)
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
