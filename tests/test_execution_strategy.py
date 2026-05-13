from __future__ import annotations

from pathlib import Path

from agentic_devloop.execution_strategy import (
    ExecutionStrategyAction,
    ExecutionStrategyReason,
    ExecutionStrategySelectorInput,
    select_execution_strategy,
)


def _base_input() -> dict[str, object]:
    return {
        "release_id": "supervisor-execution-strategy",
        "task_ids": ["task-0001"],
    }


def test_selects_one_shot_for_cohesive_single_task_scope() -> None:
    decision = select_execution_strategy(
        ExecutionStrategySelectorInput.model_validate(
            {
                **_base_input(),
                "cohesive_scope": True,
            }
        )
    )

    assert decision.selected_action == ExecutionStrategyAction.ONE_SHOT
    assert decision.reason == ExecutionStrategyReason.COHESIVE_ONE_SHOT


def test_selects_parallel_for_independent_tasks() -> None:
    decision = select_execution_strategy(
        ExecutionStrategySelectorInput.model_validate(
            {
                **_base_input(),
                "task_ids": ["task-0001", "task-0002"],
                "independent_tasks": True,
            }
        )
    )

    assert decision.selected_action == ExecutionStrategyAction.PARALLEL_CONTRACTS
    assert decision.reason == ExecutionStrategyReason.INDEPENDENT_PARALLEL


def test_selects_sequential_for_coupled_tasks() -> None:
    decision = select_execution_strategy(
        ExecutionStrategySelectorInput.model_validate(
            {
                **_base_input(),
                "task_ids": ["task-0001", "task-0002"],
                "coupled_tasks": True,
            }
        )
    )

    assert decision.selected_action == ExecutionStrategyAction.SEQUENTIAL_CONTRACTS
    assert decision.reason == ExecutionStrategyReason.COUPLED_SEQUENTIAL


def test_selects_stacked_when_stacked_dependencies_are_required() -> None:
    decision = select_execution_strategy(
        ExecutionStrategySelectorInput.model_validate(
            {
                **_base_input(),
                "task_ids": ["task-0001", "task-0002"],
                "requires_stacked_branches": True,
                "coupled_tasks": True,
            }
        )
    )

    assert decision.selected_action == ExecutionStrategyAction.STACKED_BRANCHES
    assert decision.reason == ExecutionStrategyReason.STACKED_DEPENDENCY


def test_selects_patch_handoff_when_required() -> None:
    decision = select_execution_strategy(
        ExecutionStrategySelectorInput.model_validate(
            {
                **_base_input(),
                "requires_patch_handoff": True,
                "cohesive_scope": True,
            }
        )
    )

    assert decision.selected_action == ExecutionStrategyAction.PATCH_HANDOFF
    assert decision.reason == ExecutionStrategyReason.PATCH_HANDOFF_REQUIRED


def test_stops_when_any_unsafe_scope_gate_is_triggered() -> None:
    decision = select_execution_strategy(
        ExecutionStrategySelectorInput.model_validate(
            {
                **_base_input(),
                "independent_tasks": True,
                "has_generated_artifact_changes": True,
            }
        )
    )

    assert decision.selected_action == ExecutionStrategyAction.STOP
    assert decision.reason == ExecutionStrategyReason.UNSAFE_SCOPE


def test_replans_when_explicit_replanning_flag_is_set() -> None:
    decision = select_execution_strategy(
        ExecutionStrategySelectorInput.model_validate(
            {
                **_base_input(),
                "needs_replanning": True,
            }
        )
    )

    assert decision.selected_action == ExecutionStrategyAction.REPLAN
    assert decision.reason == ExecutionStrategyReason.NEEDS_REPLAN


def test_records_consumed_optional_evidence_paths() -> None:
    decision = select_execution_strategy(
        ExecutionStrategySelectorInput.model_validate(
            {
                **_base_input(),
                "cohesive_scope": True,
                "state_review_snapshot_path": "repo_state/state_review_snapshot.json",
                "release_review_path": "runs/release_review.md",
                "release_metrics_path": "runs/release_metrics.json",
            }
        )
    )

    assert decision.consumed_evidence_paths == [
        Path("repo_state/state_review_snapshot.json"),
        Path("runs/release_review.md"),
        Path("runs/release_metrics.json"),
    ]
