from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from agentic_devloop.backlog import BacklogPlanResult, BacklogRunResult
from agentic_devloop.config import load_project_config
from agentic_devloop.execution_strategy import ExecutionStrategySelectorInput
from agentic_devloop.models import (
    BacklogEpic,
    BacklogEvidenceManifest,
    BacklogPlan,
    ReleaseObjective,
)
from agentic_devloop.orchestrator import ExecutorProtocol
from agentic_devloop.planning import PlannerBackend
from agentic_devloop.planner_backend import CodexPlannerBackend
from agentic_devloop.state_store import StateStore
from agentic_devloop.yaml_io import load_yaml_model, write_yaml_model

class PlanBacklogFn(Protocol):
    def __call__(
        self,
        *,
        project_id: str,
        goal: str,
        roadmap_path: Path,
        config_dir: Path,
        runs_dir: Path,
        objectives_dir: Path,
        write_objective: bool,
        mode: str,
        planner_backend: object | None,
        now: datetime | None,
    ) -> BacklogPlanResult: ...


class RunObjectiveFn(Protocol):
    def __call__(
        self,
        *,
        project_id: str,
        objective_path: Path,
        config_dir: Path,
        contracts_dir: Path,
        runs_dir: Path,
        planning_mode: str,
        planner_backend: PlannerBackend,
        executor: ExecutorProtocol | None,
        verification_timeout_seconds: int,
        allow_dirty: bool,
        commit_on_accept: bool,
        merge_on_accept: bool,
        push_on_accept: bool,
        release_finalize: str,
        integration_branch: str | None,
        stop_on_failure: bool,
        execution_mode: str,
        debug_keep_artifacts: bool,
        progress: Callable[[str], None] | None,
        execution_strategy_inputs: ExecutionStrategySelectorInput | dict | None,
    ) -> object: ...


def select_epic(plan: BacklogPlan, *, selected_epic_id: str | None) -> BacklogEpic:
    epic_id = selected_epic_id or plan.selected_epic_id
    if epic_id is None:
        raise ValueError("backlog plan did not select an epic and no --epic-id was provided")
    epic = next((item for item in plan.epics if item.epic_id == epic_id), None)
    if epic is None:
        raise ValueError(f"selected_epic_id not found in backlog plan: {epic_id}")
    return epic


def ensure_objective_for_epic(
    epic: BacklogEpic,
    objectives_dir: Path,
) -> tuple[ReleaseObjective, Path, bool]:
    objectives_dir.mkdir(parents=True, exist_ok=True)
    objective_path = objectives_dir / f"{epic.suggested_release_id}.yaml"
    if objective_path.exists():
        objective = load_yaml_model(objective_path, ReleaseObjective)
        if objective.release_id != epic.suggested_release_id:
            raise ValueError(
                f"objective release_id {objective.release_id!r} did not match expected {epic.suggested_release_id!r}"
            )
        return objective, objective_path, False

    objective = ReleaseObjective(
        release_id=epic.suggested_release_id,
        title=epic.title,
        objective=epic.objective,
        non_goals=[
            "Do not broaden the selected epic beyond its stated acceptance criteria.",
        ],
        acceptance_criteria=epic.acceptance_criteria,
    )
    written = write_yaml_model(objective_path, objective)
    return objective, written, True


class GovernorLoop:
    def __init__(
        self,
        *,
        plan_backlog: PlanBacklogFn,
        run_objective: RunObjectiveFn,
        state_store: StateStore | None = None,
    ) -> None:
        self._plan_backlog = plan_backlog
        self._run_objective = run_objective
        self._state_store = state_store

    def run_one_epic(
        self,
        *,
        project_id: str,
        goal: str,
        roadmap_path: Path,
        selected_epic_id: str | None,
        config_dir: Path,
        contracts_dir: Path,
        runs_dir: Path,
        objectives_dir: Path,
        mode: str,
        planner_backend: object | None,
        objective_planner_backend: PlannerBackend | None,
        executor: ExecutorProtocol | None,
        verification_timeout_seconds: int,
        allow_dirty: bool,
        commit_on_accept: bool,
        merge_on_accept: bool,
        push_on_accept: bool,
        release_finalize: str,
        integration_branch: str | None,
        stop_on_failure: bool,
        execution_mode: str,
        debug_keep_artifacts: bool,
        progress: Callable[[str], None] | None,
        now: datetime | None,
    ) -> BacklogRunResult:
        plan_result = self._plan_backlog(
            project_id=project_id,
            goal=goal,
            roadmap_path=roadmap_path,
            config_dir=config_dir,
            runs_dir=runs_dir,
            objectives_dir=objectives_dir,
            write_objective=False,
            mode=mode,
            planner_backend=planner_backend,
            now=now,
        )
        plan = plan_result.plan
        epic = select_epic(plan, selected_epic_id=selected_epic_id)
        if self._state_store is not None:
            self._state_store.mark_active_epic(epic.epic_id)
        objective, objective_path, created_objective = ensure_objective_for_epic(
            epic,
            objectives_dir,
        )

        objective_planning_mode = "strong-model"
        planner_backend_for_objective = objective_planner_backend
        if planner_backend_for_objective is None:
            config = load_project_config(project_id, config_dir, validate_repo=True)
            planner = config.model_roles.get("planner", config.executor)
            planner_backend_for_objective = CodexPlannerBackend(
                config=planner, repo_path=config.repo_path
            )

        execution_strategy_inputs = _build_execution_strategy_inputs(
            plan=plan,
            epic=epic,
            objective=objective,
            runs_dir=runs_dir,
        )
        objective_run = self._run_objective(
            project_id=project_id,
            objective_path=objective_path,
            config_dir=config_dir,
            contracts_dir=contracts_dir,
            runs_dir=runs_dir,
            planning_mode=objective_planning_mode,
            planner_backend=planner_backend_for_objective,
            executor=executor,
            verification_timeout_seconds=verification_timeout_seconds,
            allow_dirty=allow_dirty,
            commit_on_accept=commit_on_accept,
            merge_on_accept=merge_on_accept,
            push_on_accept=push_on_accept,
            release_finalize=release_finalize,
            integration_branch=integration_branch,
            stop_on_failure=stop_on_failure,
            execution_mode=execution_mode,
            debug_keep_artifacts=debug_keep_artifacts,
            progress=progress,
            execution_strategy_inputs=execution_strategy_inputs,
        )
        release = objective_run.release
        if self._state_store is not None:
            if release is not None and release.decision == "accepted":
                self._state_store.mark_completed_epic(epic.epic_id)
            else:
                self._state_store.mark_blocked_epic(epic.epic_id)

        evidence_manifest = BacklogEvidenceManifest(
            backlog_plan_path=plan_result.plan_path,
            generated_objective_path=objective_path if created_objective else None,
            contract_plan_path=objective_run.planning.plan_path,
            release_summary_path=release.summary_path if release is not None else None,
            release_metrics_path=release.metrics_path if release is not None else None,
            release_budget_path=release.budget_path if release is not None else None,
            release_tuning_path=release.tuning_path if release is not None else None,
            state_review_snapshot_path=plan.state_review_snapshot_path,
        )

        return BacklogRunResult(
            selected_epic_id=epic.epic_id,
            plan_path=plan_result.plan_path,
            backlog_plan_path=plan_result.plan_path,
            plan=plan,
            objective_path=objective_path,
            generated_objective_path=objective_path if created_objective else None,
            objective=objective,
            contract_plan_path=objective_run.planning.plan_path,
            execution_strategy_selection_path=getattr(
                objective_run.planning, "execution_strategy_selection_path", None
            ),
            supervisor_decision_path=getattr(objective_run.planning, "supervisor_decision_path", None),
            one_shot_execution_input_path=getattr(
                objective_run.planning, "one_shot_execution_input_path", None
            ),
            release_id=objective_run.release_id,
            release=release,
            release_summary_path=release.summary_path if release is not None else None,
            release_metrics_path=release.metrics_path if release is not None else None,
            release_budget_path=release.budget_path if release is not None else None,
            release_tuning_path=release.tuning_path if release is not None else None,
            evidence_manifest=evidence_manifest,
        )


def _build_execution_strategy_inputs(
    *,
    plan: BacklogPlan,
    epic: BacklogEpic,
    objective: ReleaseObjective,
    runs_dir: Path,
) -> dict[str, object]:
    """Build the current executable default strategy inputs for one-epic runs.

    Until a one-shot worker runner consumes one_shot_execution_input.json,
    run-backlog intentionally selects decomposed contract execution. Explicit
    callers can still pass cohesive_scope=True to plan-release/run-objective to
    materialize the one-shot input artifact.
    """
    prior_release_run_dir = _latest_release_run_dir(runs_dir=runs_dir, release_id=objective.release_id)
    release_review_path = (
        (prior_release_run_dir / "release_review.md") if prior_release_run_dir is not None else None
    )
    release_metrics_path = (
        (prior_release_run_dir / "release_metrics.json") if prior_release_run_dir is not None else None
    )
    return {
        "release_id": objective.release_id,
        "task_ids": [epic.epic_id],
        "coupled_tasks": True,
        "state_review_snapshot_path": plan.state_review_snapshot_path,
        "release_review_path": release_review_path if release_review_path is not None and release_review_path.exists() else None,
        "release_metrics_path": release_metrics_path if release_metrics_path is not None and release_metrics_path.exists() else None,
    }


def _latest_release_run_dir(*, runs_dir: Path, release_id: str) -> Path | None:
    if not runs_dir.exists():
        return None
    suffix = f"_{release_id}_release"
    candidates = [path for path in runs_dir.iterdir() if path.is_dir() and path.name.endswith(suffix)]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.name)
