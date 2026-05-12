from __future__ import annotations

from pathlib import Path

import pytest

from agentic_devloop.runtime_supervisor import (
    RepairActionKind,
    RepairDecisionClassification,
    RuntimeSupervisor,
    RuntimeSupervisorDecisionKind,
    RuntimeSupervisorInput,
    RuntimeSupervisorStopReason,
    BacklogStateReference,
    BudgetLedgerPaths,
    EvidenceBundlePaths,
    RawLogPaths,
    ReleaseEvent,
    ReleaseEventKind,
    ReleaseSummaryReference,
    TuningReportPaths,
)


def _input(classification: RepairDecisionClassification, *, attempt: int, max_retries: int) -> RuntimeSupervisorInput:
    return RuntimeSupervisorInput(
        classification=classification,
        attempt=attempt,
        max_retries=max_retries,
        release_event=ReleaseEvent(
            kind=ReleaseEventKind.TASK_FAILED,
            message="task failed",
            event_path=Path("runs/release/event.jsonl"),
        ),
        release_summary=ReleaseSummaryReference(
            release_id="runtime-supervisor-repair-loop",
            summary_path=Path("runs/release/release_summary.yaml"),
        ),
        evidence_bundle_paths=EvidenceBundlePaths(
            bundle_path=Path("runs/release/task_1"),
            changed_files_path=Path("runs/release/task_1/changed_files.txt"),
            verification_log_path=Path("runs/release/task_1/verification.log"),
        ),
        raw_log_paths=RawLogPaths(
            supervisor_log_path=Path("runs/release/supervisor.log"),
            worker_stdout_path=Path("runs/release/task_1/stdout.log"),
            worker_stderr_path=Path("runs/release/task_1/stderr.log"),
        ),
        budget_ledger_paths=BudgetLedgerPaths(
            repair_budget_ledger_path=Path("runs/release/repair_budget.yaml"),
            retry_budget_ledger_path=Path("runs/release/retry_budget.yaml"),
        ),
        tuning_report_paths=TuningReportPaths(
            model_tuning_report_path=Path("runs/release/model_tuning.yaml"),
            verification_tuning_report_path=Path("runs/release/verification_tuning.yaml"),
        ),
        backlog_state_reference=BacklogStateReference(
            backlog_state_path=Path("repo_state/auto_develop/backlog_state.yaml"),
            active_epic_id="runtime-supervisor-repair-loop",
        ),
    )


@pytest.mark.parametrize(
    "classification,expected_action",
    [
        (RepairDecisionClassification.VERIFICATION_ENVIRONMENT_DRIFT, RepairActionKind.ENVIRONMENT_REPAIR),
        (
            RepairDecisionClassification.PLANNER_CONTRACT_NON_NORMALIZED,
            RepairActionKind.PLANNER_CONTRACT_NORMALIZATION,
        ),
        (
            RepairDecisionClassification.TASK_SCOPE_OVERBROAD,
            RepairActionKind.TASK_SPLIT_OR_SCOPE_NARROWING,
        ),
        (RepairDecisionClassification.RELEASE_RESUMABLE, RepairActionKind.RELEASE_RESUME),
        (
            RepairDecisionClassification.LONG_RUNNING_WORKER_ACTIVE,
            RepairActionKind.LONG_RUNNING_WORKER_INSPECTION,
        ),
        (RepairDecisionClassification.MODEL_CAPABILITY_MISMATCH, RepairActionKind.MODEL_ESCALATION),
        (RepairDecisionClassification.REPO_STATE_STALE, RepairActionKind.REPO_STATE_UPDATE_PROPOSAL),
    ],
)
def test_retryable_classifications_select_expected_action(
    classification: RepairDecisionClassification,
    expected_action: RepairActionKind,
) -> None:
    supervisor = RuntimeSupervisor()

    decision = supervisor.decide(_input(classification, attempt=1, max_retries=3))

    assert decision.decision == RuntimeSupervisorDecisionKind.RETRY
    assert decision.retryable is True
    assert decision.stop_reason is None
    assert decision.remaining_retries == 2
    assert decision.action is not None
    assert decision.action.action_kind == expected_action
    assert decision.action.source_evidence_paths == _input(classification, attempt=1, max_retries=3).source_evidence_paths


@pytest.mark.parametrize(
    "classification",
    [
        RepairDecisionClassification.VERIFICATION_ENVIRONMENT_DRIFT,
        RepairDecisionClassification.PLANNER_CONTRACT_NON_NORMALIZED,
        RepairDecisionClassification.TASK_SCOPE_OVERBROAD,
        RepairDecisionClassification.RELEASE_RESUMABLE,
        RepairDecisionClassification.LONG_RUNNING_WORKER_ACTIVE,
        RepairDecisionClassification.MODEL_CAPABILITY_MISMATCH,
        RepairDecisionClassification.REPO_STATE_STALE,
    ],
)
def test_retryable_classifications_stop_when_retry_budget_exhausted(
    classification: RepairDecisionClassification,
) -> None:
    supervisor = RuntimeSupervisor()

    decision = supervisor.decide(_input(classification, attempt=3, max_retries=3))

    assert decision.decision == RuntimeSupervisorDecisionKind.STOP
    assert decision.retryable is False
    assert decision.action is None
    assert decision.stop_reason == RuntimeSupervisorStopReason.EXHAUSTED_RETRY_BUDGET
    assert decision.remaining_retries == 0


@pytest.mark.parametrize(
    "classification,expected_stop_reason",
    [
        (RepairDecisionClassification.MISSING_CREDENTIALS, RuntimeSupervisorStopReason.MISSING_CREDENTIALS),
        (
            RepairDecisionClassification.CONTRACT_BOUNDARY_VIOLATION,
            RuntimeSupervisorStopReason.CONTRACT_BOUNDARY_VIOLATION,
        ),
        (
            RepairDecisionClassification.UNSAFE_POLICY_EXPANSION,
            RuntimeSupervisorStopReason.UNSAFE_POLICY_EXPANSION,
        ),
        (
            RepairDecisionClassification.EXHAUSTED_RETRY_BUDGET,
            RuntimeSupervisorStopReason.EXHAUSTED_RETRY_BUDGET,
        ),
    ],
)
def test_non_retryable_classifications_stop_with_explicit_reason(
    classification: RepairDecisionClassification,
    expected_stop_reason: RuntimeSupervisorStopReason,
) -> None:
    supervisor = RuntimeSupervisor()

    decision = supervisor.decide(_input(classification, attempt=1, max_retries=3))

    assert decision.decision == RuntimeSupervisorDecisionKind.STOP
    assert decision.retryable is False
    assert decision.action is None
    assert decision.stop_reason == expected_stop_reason


@pytest.mark.parametrize(
    "attempt,max_retries",
    [
        (0, 1),
        (1, -1),
    ],
)
def test_invalid_retry_budget_inputs_raise(attempt: int, max_retries: int) -> None:
    supervisor = RuntimeSupervisor()

    with pytest.raises(ValueError):
        supervisor.decide(
            _input(
                RepairDecisionClassification.VERIFICATION_ENVIRONMENT_DRIFT,
                attempt=attempt,
                max_retries=max_retries,
            )
        )
