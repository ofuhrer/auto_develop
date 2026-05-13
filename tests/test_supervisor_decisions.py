from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from agentic_devloop.models import ContractNormalizationRefusalReason
from agentic_devloop.supervisor_decisions import (
    ContractNormalizationDecision,
    ContractNormalizationOutcome,
    DecisionRiskLevel,
    EnvironmentRepairDecision,
    EnvironmentRepairOutcome,
    ExecutionStrategyAction,
    ExecutionStrategyDecision,
    ExecutionStrategyOutcome,
    FindingAdjudicationOutcome,
    FindingSeverity,
    ModelOutputNormalizationAction,
    ModelOutputNormalizationDecision,
    ModelOutputNormalizationOutcome,
    ReleaseSchedulingDecision,
    ReleaseSchedulingAction,
    ReleaseSchedulingStalenessInputs,
    RepairLoopContinuationDecision,
    RepairLoopOutcome,
    SCHEMA_VERSION_V1,
    SchedulingOutcome,
    SoftBudgetAcceptanceDecision,
    SupervisorDecisionType,
    parse_supervisor_decision,
)


BASE = {
    "schema_version": SCHEMA_VERSION_V1,
    "decision_id": "decision-001",
    "release_id": "supervisor-decision-records",
    "decided_at": datetime(2026, 5, 13, 8, 0, 0),
    "decided_by": "supervisor-agent",
    "rationale": "Evidence supports this action.",
}


def test_parse_release_scheduling_decision() -> None:
    payload = {
        **BASE,
        "decision_type": SupervisorDecisionType.RELEASE_SCHEDULING,
        "risk_level": DecisionRiskLevel.MODERATE,
        "overlap_findings": ["src/release.py"],
        "selected_action": ReleaseSchedulingAction.SEQUENTIAL,
        "outcome": SchedulingOutcome.PROCEED_SEQUENTIAL,
        "fallback_plan": "Rerun overlap analysis before parallelizing the release.",
        "validators_to_rerun": ["overlap_report", "verification"],
        "staleness_inputs": {
            "execution_mode": "parallel",
            "selected_task_ids": ["demo-0001", "demo-0002"],
            "selected_contract_paths": ["/tmp/contracts/demo-0001.yaml", "/tmp/contracts/demo-0002.yaml"],
            "overlap_report_sha256": "abc123",
            "base_branch_head_commit": "deadbeef",
            "release_inputs_sha256": "f00d",
        },
    }

    decision = parse_supervisor_decision(payload)

    assert isinstance(decision, ReleaseSchedulingDecision)
    assert decision.schema_version == SCHEMA_VERSION_V1
    assert decision.decision_type == SupervisorDecisionType.RELEASE_SCHEDULING
    assert decision.selected_action == ReleaseSchedulingAction.SEQUENTIAL
    assert decision.fallback_plan.startswith("Rerun overlap analysis")
    assert isinstance(decision.staleness_inputs, ReleaseSchedulingStalenessInputs)


def test_parse_execution_strategy_decision() -> None:
    payload = {
        **BASE,
        "decision_type": SupervisorDecisionType.EXECUTION_STRATEGY,
        "risk_level": DecisionRiskLevel.MODERATE,
        "selected_action": ExecutionStrategyAction.ONE_SHOT,
        "outcome": ExecutionStrategyOutcome.PROCEED_ONE_SHOT,
        "fallback_plan": "Decompose into sequential contracts if one-shot verification fails.",
        "validators_to_rerun": ["contract_plan", "verification"],
    }

    decision = parse_supervisor_decision(payload)

    assert isinstance(decision, ExecutionStrategyDecision)
    assert decision.decision_type == SupervisorDecisionType.EXECUTION_STRATEGY
    assert decision.selected_action == ExecutionStrategyAction.ONE_SHOT


def test_execution_strategy_rejects_invalid_action_outcome_combination() -> None:
    with pytest.raises(ValidationError, match="selected_action must match outcome"):
        ExecutionStrategyDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.EXECUTION_STRATEGY,
                "risk_level": DecisionRiskLevel.MODERATE,
                "selected_action": ExecutionStrategyAction.PARALLEL_CONTRACTS,
                "outcome": ExecutionStrategyOutcome.PROCEED_SEQUENTIAL,
                "fallback_plan": "Retry strategy selection with updated coupling evidence.",
                "validators_to_rerun": ["contract_plan", "verification"],
            }
        )


def test_execution_strategy_requires_fallback_and_validators() -> None:
    with pytest.raises(ValidationError):
        ExecutionStrategyDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.EXECUTION_STRATEGY,
                "risk_level": DecisionRiskLevel.MODERATE,
                "selected_action": ExecutionStrategyAction.REPLAN,
                "outcome": ExecutionStrategyOutcome.REPLAN,
                "fallback_plan": "",
                "validators_to_rerun": ["contract_plan"],
            }
        )

    with pytest.raises(ValidationError, match="validators to rerun must not be empty"):
        ExecutionStrategyDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.EXECUTION_STRATEGY,
                "risk_level": DecisionRiskLevel.MODERATE,
                "selected_action": ExecutionStrategyAction.REPLAN,
                "outcome": ExecutionStrategyOutcome.REPLAN,
                "fallback_plan": "Escalate to replanning.",
                "validators_to_rerun": [],
            }
        )


def test_repair_loop_attempt_must_not_exceed_max_attempts() -> None:
    with pytest.raises(ValidationError, match="attempt must be less than or equal"):
        RepairLoopContinuationDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.REPAIR_LOOP_CONTINUATION,
                "risk_level": DecisionRiskLevel.HIGH,
                "attempt": 4,
                "max_attempts": 3,
                "outcome": RepairLoopOutcome.STOP,
            }
        )


def test_soft_budget_acceptance_requires_actual_at_or_above_limit() -> None:
    with pytest.raises(ValidationError, match="actual must be greater than or equal"):
        SoftBudgetAcceptanceDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.SOFT_BUDGET_ACCEPTANCE,
                "budget_name": "max_changed_files_per_task",
                "configured_limit": 8,
                "actual": 7,
                "outcome": "accept_overage",
            }
        )


def test_contract_normalization_decision_requires_outcome_consistency() -> None:
    with pytest.raises(ValidationError, match="requires changed_fields"):
        ContractNormalizationDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.CONTRACT_NORMALIZATION,
                "outcome": ContractNormalizationOutcome.NORMALIZE_AND_RETRY,
                "changed_fields": [],
                "refusal_reasons": [],
            }
        )

    with pytest.raises(ValidationError, match="requires refusal_reasons"):
        ContractNormalizationDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.CONTRACT_NORMALIZATION,
                "outcome": ContractNormalizationOutcome.REFUSE_AND_STOP,
                "changed_fields": [],
                "refusal_reasons": [],
            }
        )


def test_contract_normalization_refusal_reason_is_typed() -> None:
    decision = ContractNormalizationDecision.model_validate(
        {
            **BASE,
            "decision_type": SupervisorDecisionType.CONTRACT_NORMALIZATION,
            "outcome": ContractNormalizationOutcome.REFUSE_AND_STOP,
            "changed_fields": [],
            "refusal_reasons": [ContractNormalizationRefusalReason.UNSAFE_NORMALIZATION],
        }
    )

    assert decision.refusal_reasons == [ContractNormalizationRefusalReason.UNSAFE_NORMALIZATION]


def test_contract_normalization_refusal_reason_supports_missing_required_evidence() -> None:
    decision = ContractNormalizationDecision.model_validate(
        {
            **BASE,
            "decision_type": SupervisorDecisionType.CONTRACT_NORMALIZATION,
            "outcome": ContractNormalizationOutcome.REFUSE_AND_STOP,
            "changed_fields": [],
            "refusal_reasons": [ContractNormalizationRefusalReason.MISSING_REQUIRED_EVIDENCE],
        }
    )

    assert decision.refusal_reasons == [ContractNormalizationRefusalReason.MISSING_REQUIRED_EVIDENCE]


def test_parse_review_finding_adjudication_decision() -> None:
    payload = {
        **BASE,
        "decision_type": SupervisorDecisionType.REVIEW_FINDING_ADJUDICATION,
        "finding_id": "fr-123",
        "severity": FindingSeverity.HIGH,
        "outcome": FindingAdjudicationOutcome.REQUIRED_REPAIR,
        "repair_task_ids": ["repair-0001"],
    }

    decision = parse_supervisor_decision(payload)

    assert decision.decision_type == SupervisorDecisionType.REVIEW_FINDING_ADJUDICATION


def test_parse_environment_repair_decision() -> None:
    payload = {
        **BASE,
        "decision_type": SupervisorDecisionType.ENVIRONMENT_REPAIR,
        "outcome": EnvironmentRepairOutcome.APPLY_AND_RETRY,
        "capture_commands": ["python -V", "pip list"],
    }

    decision = parse_supervisor_decision(payload)

    assert isinstance(decision, EnvironmentRepairDecision)
    assert decision.outcome == EnvironmentRepairOutcome.APPLY_AND_RETRY


def test_parse_model_output_normalization_decision() -> None:
    payload = {
        **BASE,
        "decision_type": SupervisorDecisionType.MODEL_OUTPUT_NORMALIZATION,
        "risk_level": DecisionRiskLevel.HIGH,
        "raw_artifact_paths": ["runs/release/reviewer_raw.json"],
        "validation_errors": [
            {
                "field": "findings[0].evidence_paths",
                "message": "Field required",
                "error_type": "missing",
            }
        ],
        "selected_action": ModelOutputNormalizationAction.APPLY_NORMALIZATION,
        "outcome": ModelOutputNormalizationOutcome.NORMALIZED_AND_RETRY,
        "fallback_plan": "Refuse and stop if rerun validation still fails.",
        "validators_to_rerun": ["review_findings_schema", "release_review_gate"],
        "normalized_artifact_path": "runs/release/feature_review.normalized.json",
    }

    decision = parse_supervisor_decision(payload)

    assert isinstance(decision, ModelOutputNormalizationDecision)
    assert decision.decision_type == SupervisorDecisionType.MODEL_OUTPUT_NORMALIZATION
    assert decision.selected_action == ModelOutputNormalizationAction.APPLY_NORMALIZATION
    assert decision.outcome == ModelOutputNormalizationOutcome.NORMALIZED_AND_RETRY


def test_model_output_normalization_requires_consistent_outcome_fields() -> None:
    with pytest.raises(ValidationError, match="selected_action must match outcome"):
        ModelOutputNormalizationDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.MODEL_OUTPUT_NORMALIZATION,
                "risk_level": DecisionRiskLevel.HIGH,
                "raw_artifact_paths": ["runs/release/reviewer_raw.json"],
                "validation_errors": [
                    {
                        "field": "findings[0].evidence_paths",
                        "message": "Field required",
                        "error_type": "missing",
                    }
                ],
                "selected_action": ModelOutputNormalizationAction.REFUSE,
                "outcome": ModelOutputNormalizationOutcome.NORMALIZED_AND_RETRY,
                "fallback_plan": "Escalate decisioning to stop path.",
                "validators_to_rerun": ["review_findings_schema"],
                "normalized_artifact_path": "runs/release/feature_review.normalized.json",
            }
        )

    with pytest.raises(ValidationError, match="requires normalized_artifact_path"):
        ModelOutputNormalizationDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.MODEL_OUTPUT_NORMALIZATION,
                "risk_level": DecisionRiskLevel.HIGH,
                "raw_artifact_paths": ["runs/release/reviewer_raw.json"],
                "validation_errors": [
                    {
                        "field": "findings[0].evidence_paths",
                        "message": "Field required",
                        "error_type": "missing",
                    }
                ],
                "selected_action": ModelOutputNormalizationAction.APPLY_NORMALIZATION,
                "outcome": ModelOutputNormalizationOutcome.NORMALIZED_AND_RETRY,
                "fallback_plan": "Escalate decisioning to stop path.",
                "validators_to_rerun": ["review_findings_schema"],
            }
        )

    with pytest.raises(ValidationError, match="requires validation_errors"):
        ModelOutputNormalizationDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.MODEL_OUTPUT_NORMALIZATION,
                "risk_level": DecisionRiskLevel.HIGH,
                "raw_artifact_paths": ["runs/release/reviewer_raw.json"],
                "validation_errors": [],
                "selected_action": ModelOutputNormalizationAction.APPLY_NORMALIZATION,
                "outcome": ModelOutputNormalizationOutcome.NORMALIZED_AND_RETRY,
                "fallback_plan": "Escalate decisioning to stop path.",
                "validators_to_rerun": ["review_findings_schema"],
                "normalized_artifact_path": "runs/release/feature_review.normalized.json",
            }
        )

    with pytest.raises(ValidationError, match="requires validators_to_rerun"):
        ModelOutputNormalizationDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.MODEL_OUTPUT_NORMALIZATION,
                "risk_level": DecisionRiskLevel.HIGH,
                "raw_artifact_paths": ["runs/release/reviewer_raw.json"],
                "validation_errors": [
                    {
                        "field": "findings[0].evidence_paths",
                        "message": "Field required",
                        "error_type": "missing",
                    }
                ],
                "selected_action": ModelOutputNormalizationAction.APPLY_NORMALIZATION,
                "outcome": ModelOutputNormalizationOutcome.NORMALIZED_AND_RETRY,
                "fallback_plan": "Escalate decisioning to stop path.",
                "validators_to_rerun": [],
                "normalized_artifact_path": "runs/release/feature_review.normalized.json",
            }
        )

    with pytest.raises(ValidationError, match="requires refusal_reason"):
        ModelOutputNormalizationDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.MODEL_OUTPUT_NORMALIZATION,
                "risk_level": DecisionRiskLevel.HIGH,
                "raw_artifact_paths": ["runs/release/reviewer_raw.json"],
                "validation_errors": [
                    {
                        "field": "findings[0].evidence_paths",
                        "message": "Field required",
                        "error_type": "missing",
                    }
                ],
                "selected_action": ModelOutputNormalizationAction.REFUSE,
                "outcome": ModelOutputNormalizationOutcome.REFUSED_AND_STOP,
                "fallback_plan": "Escalate decisioning to stop path.",
                "validators_to_rerun": ["review_findings_schema"],
            }
        )


def test_model_output_normalization_refused_allows_empty_validation_and_rerun_lists() -> None:
    decision = ModelOutputNormalizationDecision.model_validate(
        {
            **BASE,
            "decision_type": SupervisorDecisionType.MODEL_OUTPUT_NORMALIZATION,
            "risk_level": DecisionRiskLevel.HIGH,
            "raw_artifact_paths": ["runs/release/reviewer_raw.json"],
            "validation_errors": [],
            "selected_action": ModelOutputNormalizationAction.REFUSE,
            "outcome": ModelOutputNormalizationOutcome.REFUSED_AND_STOP,
            "fallback_plan": "Escalate decisioning to stop path.",
            "validators_to_rerun": [],
            "refusal_reason": "Insufficient confidence in safe normalization.",
        }
    )

    assert decision.outcome == ModelOutputNormalizationOutcome.REFUSED_AND_STOP


def test_parse_supervisor_decision_rejects_unsupported_type() -> None:
    payload = {
        **BASE,
        "decision_type": "not_a_real_decision_type",
        "risk_level": DecisionRiskLevel.LOW,
        "overlap_findings": [],
        "outcome": SchedulingOutcome.PROCEED_PARALLEL,
    }

    with pytest.raises(ValidationError):
        parse_supervisor_decision(payload)


def test_invalid_schema_version_is_rejected() -> None:
    payload = {
        **BASE,
        "schema_version": "2.0",
        "decision_type": SupervisorDecisionType.RELEASE_SCHEDULING,
        "risk_level": DecisionRiskLevel.LOW,
        "overlap_findings": [],
        "outcome": SchedulingOutcome.PROCEED_PARALLEL,
    }

    with pytest.raises(ValidationError):
        parse_supervisor_decision(payload)


def test_execution_strategy_invalid_schema_version_is_rejected() -> None:
    payload = {
        **BASE,
        "schema_version": "2.0",
        "decision_type": SupervisorDecisionType.EXECUTION_STRATEGY,
        "risk_level": DecisionRiskLevel.LOW,
        "selected_action": ExecutionStrategyAction.REPLAN,
        "outcome": ExecutionStrategyOutcome.REPLAN,
        "fallback_plan": "Escalate strategy selection for manual review.",
        "validators_to_rerun": ["contract_plan"],
    }

    with pytest.raises(ValidationError):
        parse_supervisor_decision(payload)
