from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from agentic_devloop.runtime_supervisor import (
    RuntimeSupervisorApplierStopKind,
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
from agentic_devloop.models import ModelOutputNormalizationActionPayload, TaskContract
from agentic_devloop.supervisor_decisions import DecisionRiskLevel


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


def _contract(*, allowed_files: list[str]) -> TaskContract:
    return TaskContract.model_validate(
        {
            "task_id": "rs-0002",
            "release_id": "runtime-supervisor-repair-loop",
            "title": "Runtime supervisor repair",
            "task_type": "code_only",
            "budget_class": "M",
            "objective": "Repair",
            "allowed_files": allowed_files,
            "required_evidence": ["git diff"],
            "verification": {"profile": "code_only"},
            "stop_conditions": ["stop"],
        }
    )


def test_task_scope_narrowing_stops_when_scope_broadens() -> None:
    supervisor = RuntimeSupervisor()
    source_paths = _input(RepairDecisionClassification.TASK_SCOPE_OVERBROAD, attempt=1, max_retries=3).source_evidence_paths

    result = supervisor.apply_task_split_or_scope_narrowing(
        source_evidence_paths=source_paths,
        original_contract=_contract(allowed_files=["src/a.py", "tests/a.py"]),
        narrowed_allowed_files=["src/a.py", "docs/new.md"],
    )

    assert result.applied is False
    assert result.stop_evidence is not None
    assert result.stop_evidence.kind == RuntimeSupervisorApplierStopKind.BROADENS_ALLOWED_FILES


def test_model_escalation_stops_when_retry_budget_exhausted() -> None:
    supervisor = RuntimeSupervisor()
    source_paths = _input(
        RepairDecisionClassification.MODEL_CAPABILITY_MISMATCH,
        attempt=3,
        max_retries=3,
    ).source_evidence_paths

    result = supervisor.apply_model_escalation_recommendation(
        source_evidence_paths=source_paths,
        current_model="gpt-5.4-mini",
        recommended_model="gpt-5.5",
        reason="executor failure",
        retry_budget_remaining=0,
    )

    assert result.applied is False
    assert result.stop_evidence is not None
    assert result.stop_evidence.kind == RuntimeSupervisorApplierStopKind.EXCEEDS_RETRY_BUDGET


def test_model_escalation_applies_when_model_available_and_budget_remaining() -> None:
    supervisor = RuntimeSupervisor()
    source_paths = _input(
        RepairDecisionClassification.MODEL_CAPABILITY_MISMATCH,
        attempt=1,
        max_retries=3,
    ).source_evidence_paths

    result = supervisor.apply_model_escalation_recommendation(
        source_evidence_paths=source_paths,
        current_model="gpt-5.4-mini",
        recommended_model="gpt-5.5",
        reason="executor failure",
        retry_budget_remaining=1,
        available_models=["gpt-5.4-mini", "gpt-5.5"],
    )

    assert result.applied is True
    assert result.stop_evidence is None
    assert result.proposal is not None
    assert result.proposal.recommended_model == "gpt-5.5"


def test_model_escalation_stops_when_recommended_model_unavailable() -> None:
    supervisor = RuntimeSupervisor()
    source_paths = _input(
        RepairDecisionClassification.MODEL_CAPABILITY_MISMATCH,
        attempt=1,
        max_retries=3,
    ).source_evidence_paths

    result = supervisor.apply_model_escalation_recommendation(
        source_evidence_paths=source_paths,
        current_model="gpt-5.4-mini",
        recommended_model="gpt-5.5",
        reason="executor failure",
        retry_budget_remaining=1,
        available_models=["gpt-5.4-mini"],
    )

    assert result.applied is False
    assert result.stop_evidence is not None
    assert result.stop_evidence.kind == RuntimeSupervisorApplierStopKind.UNAVAILABLE_MODEL


def test_release_resume_stops_without_hard_gate_fields() -> None:
    supervisor = RuntimeSupervisor()
    source_paths = _input(RepairDecisionClassification.RELEASE_RESUMABLE, attempt=1, max_retries=3).source_evidence_paths

    result = supervisor.apply_release_resume_intent(
        source_evidence_paths=source_paths,
        action_id=None,
        retry_budget=1,
        stop_reason_fallback=RuntimeSupervisorStopReason.EXHAUSTED_RETRY_BUDGET,
    )

    assert result.applied is False
    assert result.stop_evidence is not None
    assert result.stop_evidence.kind == RuntimeSupervisorApplierStopKind.BYPASSES_HARD_GATE


def test_planner_contract_normalization_produces_typed_proposal() -> None:
    supervisor = RuntimeSupervisor()
    source_paths = _input(
        RepairDecisionClassification.PLANNER_CONTRACT_NON_NORMALIZED,
        attempt=1,
        max_retries=3,
    ).source_evidence_paths
    valid_plan = {
        "release_id": "runtime-supervisor-repair-loop",
        "planner": "strong-model",
        "generated_contracts": [
            {
                "task_id": "rs-0002",
                "title": "title",
                "objective": "objective",
                "rationale": "rationale",
                "suggested_contract": _contract(allowed_files=["src/agentic_devloop/runtime_supervisor.py"]).model_dump(
                    mode="python"
                ),
            }
        ],
        "warnings": ["schema normalized"],
    }

    result = supervisor.apply_planner_contract_normalization(
        source_evidence_paths=source_paths,
        expected_release_id="runtime-supervisor-repair-loop",
        candidate_plan=valid_plan,
    )

    assert result.applied is True
    assert result.proposal is not None
    assert result.proposal.action_kind == RepairActionKind.PLANNER_CONTRACT_NORMALIZATION


def test_model_output_normalization_decision_produces_typed_proposal() -> None:
    supervisor = RuntimeSupervisor()
    source_paths = _input(
        RepairDecisionClassification.PLANNER_CONTRACT_NON_NORMALIZED,
        attempt=1,
        max_retries=3,
    ).source_evidence_paths
    action_payload = ModelOutputNormalizationActionPayload.model_validate(
        {
            "raw_artifact_paths": [Path("planner.raw.json")],
            "validation_errors": [
                {"field": "generated_contracts[0].task_id", "message": "Field required", "error_type": "missing"}
            ],
            "selected_action": "apply_normalization",
            "outcome": "normalized_and_retry",
            "rationale": "Output is useful but missing required fields.",
            "fallback_plan": "Refuse and stop if normalized output does not validate.",
            "validators_to_rerun": ["contract_plan", "verification"],
            "normalized_artifact_path": Path("planner.normalized.json"),
        }
    )

    result = supervisor.apply_model_output_normalization_decision(
        source_evidence_paths=source_paths,
        decision_id="normalization-typed-001",
        release_id="planner-normalization-generalization",
        decided_by="supervisor-agent",
        risk_level=DecisionRiskLevel.MODERATE,
        evidence_paths=[Path("normalization-evidence.log")],
        action_payload=action_payload,
        decided_at=datetime(2026, 5, 13, 9, 0, 0),
    )

    assert result.applied is True
    assert result.stop_evidence is None
    assert result.proposal is not None
    assert result.proposal.action_kind == RepairActionKind.PLANNER_CONTRACT_NORMALIZATION
    assert result.proposal.decision.outcome.value == "normalized_and_retry"


def test_planner_contract_normalization_repairs_wrapper_and_alias_drift() -> None:
    supervisor = RuntimeSupervisor()
    source_paths = _input(
        RepairDecisionClassification.PLANNER_CONTRACT_NON_NORMALIZED,
        attempt=1,
        max_retries=3,
    ).source_evidence_paths

    candidate_plan = {
        "release_id": "planner-normalization-generalization",
        "planner": "strong-model",
        "generated_contracts": [
            {
                "task_id": "demo-0001",
                "title": "Wrapper drift",
                "objective": "Wrapper drift",
                "rationale": "Wrapper drift",
                "depends_on": ["demo-0000"],
                "suggested_contract": {
                    "task_id": "demo-0001",
                    "title": "Wrapper drift",
                    "task_type": "docs_and_tests",
                    "budget_class": "S",
                    "objective": "Wrapper drift",
                    "allowedFiles": ["src/agentic_devloop/runtime_supervisor.py"],
                    "forbiddenChanges": ["Do not widen scope."],
                    "verification": ["true"],
                    "stopConditions": ["Stop when scope expands."],
                    "requirements": ["remove me"],
                    "implementation_requirements": ["Keep it bounded."],
                },
            }
        ],
        "warnings": [],
    }

    result = supervisor.apply_planner_contract_normalization(
        source_evidence_paths=source_paths,
        expected_release_id="planner-normalization-generalization",
        candidate_plan=candidate_plan,
    )

    assert result.applied is True
    assert result.proposal is not None
    normalized_plan = result.proposal.normalized_plan
    normalized_contract = normalized_plan.generated_contracts[0].suggested_contract

    assert normalized_contract.release_id == "planner-normalization-generalization"
    assert normalized_contract.task_type.value == "release_preparation"
    assert normalized_contract.depends_on == ["demo-0000"]
    assert "git diff" in normalized_contract.required_evidence
    assert "changed-files list" in normalized_contract.required_evidence
    assert any("planner_contract_payload_normalization=" in warning for warning in normalized_plan.warnings)
    assert "Implementation requirements:" in normalized_contract.objective
