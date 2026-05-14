from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator

from agentic_devloop.models import StrictModel


class ExecutionStrategyAction(StrEnum):
    ONE_SHOT = "one_shot"
    SEQUENTIAL_CONTRACTS = "sequential_contracts"
    PARALLEL_CONTRACTS = "parallel_contracts"
    STACKED_BRANCHES = "stacked_branches"
    PATCH_HANDOFF = "patch_handoff"
    REPLAN = "replan"
    STOP = "stop"


class ExecutionStrategyReason(StrEnum):
    COHESIVE_ONE_SHOT = "cohesive_one_shot"
    INDEPENDENT_PARALLEL = "independent_parallel"
    COUPLED_SEQUENTIAL = "coupled_sequential"
    STACKED_DEPENDENCY = "stacked_dependency"
    PATCH_HANDOFF_REQUIRED = "patch_handoff_required"
    NEEDS_REPLAN = "needs_replan"
    UNSAFE_SCOPE = "unsafe_scope"


class ExecutionStrategySelectorInput(StrictModel):
    release_id: str = Field(min_length=1)
    task_ids: list[str] = Field(min_length=1)

    cohesive_scope: bool = False
    independent_tasks: bool = False
    coupled_tasks: bool = False
    requires_stacked_branches: bool = False
    requires_patch_handoff: bool = False
    needs_replanning: bool = False

    has_forbidden_path_changes: bool = False
    has_generated_artifact_changes: bool = False
    has_lockfile_changes: bool = False
    has_migration_changes: bool = False
    unsafe_policy_expansion: bool = False
    missing_required_evidence: bool = False
    verification_failed: bool = False
    finalization_policy_blocked: bool = False

    state_review_snapshot_path: Path | None = None
    release_review_path: Path | None = None
    release_metrics_path: Path | None = None
    cost_runtime_governance_decision_path: Path | None = None

    @model_validator(mode="after")
    def task_ids_must_be_non_empty(self) -> "ExecutionStrategySelectorInput":
        if any(not task_id.strip() for task_id in self.task_ids):
            raise ValueError("task IDs must not be empty")
        return self


class ExecutionStrategySelection(StrictModel):
    release_id: str = Field(min_length=1)
    selected_action: ExecutionStrategyAction
    reason: ExecutionStrategyReason
    consumed_evidence_paths: list[Path] = Field(default_factory=list)


def select_execution_strategy(inputs: ExecutionStrategySelectorInput) -> ExecutionStrategySelection:
    consumed_evidence_paths = _consumed_optional_evidence_paths(inputs)

    if _has_unsafe_scope(inputs):
        return ExecutionStrategySelection(
            release_id=inputs.release_id,
            selected_action=ExecutionStrategyAction.STOP,
            reason=ExecutionStrategyReason.UNSAFE_SCOPE,
            consumed_evidence_paths=consumed_evidence_paths,
        )

    if inputs.requires_patch_handoff:
        return ExecutionStrategySelection(
            release_id=inputs.release_id,
            selected_action=ExecutionStrategyAction.PATCH_HANDOFF,
            reason=ExecutionStrategyReason.PATCH_HANDOFF_REQUIRED,
            consumed_evidence_paths=consumed_evidence_paths,
        )

    if inputs.requires_stacked_branches:
        return ExecutionStrategySelection(
            release_id=inputs.release_id,
            selected_action=ExecutionStrategyAction.STACKED_BRANCHES,
            reason=ExecutionStrategyReason.STACKED_DEPENDENCY,
            consumed_evidence_paths=consumed_evidence_paths,
        )

    if inputs.needs_replanning:
        return ExecutionStrategySelection(
            release_id=inputs.release_id,
            selected_action=ExecutionStrategyAction.REPLAN,
            reason=ExecutionStrategyReason.NEEDS_REPLAN,
            consumed_evidence_paths=consumed_evidence_paths,
        )

    if inputs.independent_tasks and not inputs.coupled_tasks:
        return ExecutionStrategySelection(
            release_id=inputs.release_id,
            selected_action=ExecutionStrategyAction.PARALLEL_CONTRACTS,
            reason=ExecutionStrategyReason.INDEPENDENT_PARALLEL,
            consumed_evidence_paths=consumed_evidence_paths,
        )

    if inputs.coupled_tasks:
        return ExecutionStrategySelection(
            release_id=inputs.release_id,
            selected_action=ExecutionStrategyAction.SEQUENTIAL_CONTRACTS,
            reason=ExecutionStrategyReason.COUPLED_SEQUENTIAL,
            consumed_evidence_paths=consumed_evidence_paths,
        )

    if inputs.cohesive_scope and len(inputs.task_ids) == 1:
        return ExecutionStrategySelection(
            release_id=inputs.release_id,
            selected_action=ExecutionStrategyAction.ONE_SHOT,
            reason=ExecutionStrategyReason.COHESIVE_ONE_SHOT,
            consumed_evidence_paths=consumed_evidence_paths,
        )

    return ExecutionStrategySelection(
        release_id=inputs.release_id,
        selected_action=ExecutionStrategyAction.REPLAN,
        reason=ExecutionStrategyReason.NEEDS_REPLAN,
        consumed_evidence_paths=consumed_evidence_paths,
    )


def _has_unsafe_scope(inputs: ExecutionStrategySelectorInput) -> bool:
    return any(
        (
            inputs.has_forbidden_path_changes,
            inputs.has_generated_artifact_changes,
            inputs.has_lockfile_changes,
            inputs.has_migration_changes,
            inputs.unsafe_policy_expansion,
            inputs.missing_required_evidence,
            inputs.verification_failed,
            inputs.finalization_policy_blocked,
        )
    )


def _consumed_optional_evidence_paths(inputs: ExecutionStrategySelectorInput) -> list[Path]:
    return [
        path
        for path in (
            inputs.state_review_snapshot_path,
            inputs.release_review_path,
            inputs.release_metrics_path,
            inputs.cost_runtime_governance_decision_path,
        )
        if path is not None
    ]
