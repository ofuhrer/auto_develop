from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agentic_devloop.orchestrator import ExecutorProtocol
from agentic_devloop.planning import ContractPlanResult, PlannerBackend, plan_release_contracts
from agentic_devloop.release import ReleaseRunResult, run_release


@dataclass(frozen=True)
class ObjectiveRunResult:
    release_id: str
    planning: ContractPlanResult
    release: ReleaseRunResult


def run_objective(
    *,
    project_id: str,
    objective_path: Path,
    config_dir: Path = Path("configs"),
    contracts_dir: Path = Path("contracts"),
    runs_dir: Path = Path("runs"),
    planning_mode: str = "deterministic",
    planner_backend: PlannerBackend | None = None,
    executor: ExecutorProtocol | None = None,
    verification_timeout_seconds: int = 600,
    allow_dirty: bool = False,
    commit_on_accept: bool = False,
    merge_on_accept: bool = False,
    push_on_accept: bool = False,
    stop_on_failure: bool = True,
    execution_mode: str = "sequential",
    debug_keep_artifacts: bool = False,
    progress: Callable[[str], None] | None = None,
) -> ObjectiveRunResult:
    planning = plan_release_contracts(
        objective_path=objective_path,
        contracts_dir=contracts_dir,
        runs_dir=runs_dir,
        write_contracts_dir=contracts_dir,
        mode=planning_mode,
        project_id=project_id if planning_mode == "strong-model" else None,
        config_dir=config_dir,
        planner_backend=planner_backend,
    )
    if not planning.written_contract_paths:
        raise ValueError("objective planning produced no runnable contracts")

    release = run_release(
        project_id=project_id,
        release_id=planning.release_id,
        contract_paths=planning.written_contract_paths,
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=runs_dir,
        executor=executor,
        verification_timeout_seconds=verification_timeout_seconds,
        allow_dirty=allow_dirty,
        commit_on_accept=commit_on_accept,
        merge_on_accept=merge_on_accept,
        push_on_accept=push_on_accept,
        stop_on_failure=stop_on_failure,
        execution_mode=execution_mode,
        debug_keep_artifacts=debug_keep_artifacts,
        progress=progress,
    )
    return ObjectiveRunResult(release_id=planning.release_id, planning=planning, release=release)
