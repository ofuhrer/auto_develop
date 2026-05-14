from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from agentic_devloop.models import ContractNormalizationRefusalReason
from agentic_devloop.models import ReleaseFinalizationPolicyName
from agentic_devloop.supervisor_decisions import (
    ContractNormalizationDecision,
    ContractNormalizationOutcome,
    DecisionRiskLevel,
    EnvironmentRepairDecision,
    EnvironmentRepairOutcome,
    FinalReviewFindingAdjudicationAction,
    FinalReviewFindingAdjudicationClassification,
    FinalReviewFindingAdjudicationDecision,
    FinalReviewFindingAdjudicationOutcome,
    ExecutionStrategyAction,
    ExecutionStrategyDecision,
    ExecutionStrategyOutcome,
    FeatureReviewFindingAction,
    FeatureReviewFindingClassification,
    FeatureReviewFindingClassificationDecision,
    FeatureReviewFindingOutcome,
    FindingAdjudicationOutcome,
    FindingSeverity,
    ModelOutputNormalizationAction,
    ModelOutputNormalizationDecision,
    ModelOutputNormalizationOutcome,
    ReleaseSchedulingDecision,
    ReleaseSchedulingAction,
    ReleaseSchedulingStalenessInputs,
    ReleaseFinalizationDecision,
    ReleaseFinalizationOutcome,
    RepairLoopContinuationDecision,
    RepairLoopOutcome,
    SCHEMA_VERSION_V1,
    SchedulingOutcome,
    ScopeRiskAction,
    ScopeRiskAffectedScope,
    ScopeRiskBudgetPolicyDecision,
    ScopeRiskClassification,
    ScopeRiskOutcome,
    SoftBudgetAcceptanceDecision,
    SupervisorDecisionType,
    LEGACY_VALIDATORS_UNSPECIFIED,
    effective_validators_to_rerun,
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


def test_parse_release_finalization_decision_local_merge() -> None:
    payload = {
        **BASE,
        "decision_type": SupervisorDecisionType.RELEASE_FINALIZATION,
        "risk_level": DecisionRiskLevel.LOW,
        "policy_basis": ReleaseFinalizationPolicyName.LOCAL_MERGE,
        "selected_action": ReleaseFinalizationPolicyName.LOCAL_MERGE,
        "outcome": ReleaseFinalizationOutcome.LOCAL_MERGE,
        "fallback_plan": "Stop and escalate if merge prerequisites fail.",
        "validators_to_rerun": ["finalization_gate", "integration_verification"],
        "outcome_references": ["runs/release/finalization_summary.json"],
    }

    decision = parse_supervisor_decision(payload)

    assert isinstance(decision, ReleaseFinalizationDecision)
    assert decision.outcome == ReleaseFinalizationOutcome.LOCAL_MERGE


def test_parse_release_finalization_decision_push_feature() -> None:
    payload = {
        **BASE,
        "decision_type": SupervisorDecisionType.RELEASE_FINALIZATION,
        "risk_level": DecisionRiskLevel.MODERATE,
        "policy_basis": ReleaseFinalizationPolicyName.PUSH_FEATURE,
        "selected_action": ReleaseFinalizationPolicyName.PUSH_FEATURE,
        "outcome": ReleaseFinalizationOutcome.PUSH_FEATURE,
        "fallback_plan": "Prepare PR-only outcome if push fails.",
        "validators_to_rerun": ["finalization_gate", "credentials_check"],
        "outcome_references": ["runs/release/finalization_summary.json"],
    }

    decision = parse_supervisor_decision(payload)

    assert isinstance(decision, ReleaseFinalizationDecision)
    assert decision.outcome == ReleaseFinalizationOutcome.PUSH_FEATURE


def test_parse_release_finalization_decision_pr_preparation() -> None:
    payload = {
        **BASE,
        "decision_type": SupervisorDecisionType.RELEASE_FINALIZATION,
        "risk_level": DecisionRiskLevel.MODERATE,
        "policy_basis": ReleaseFinalizationPolicyName.PR_PREPARATION,
        "selected_action": ReleaseFinalizationPolicyName.PR_PREPARATION,
        "outcome": ReleaseFinalizationOutcome.PR_PREPARATION,
        "fallback_plan": "Stop if PR metadata generation fails validation.",
        "validators_to_rerun": ["finalization_gate", "review_status"],
        "outcome_references": ["runs/release/finalization_summary.json"],
    }

    decision = parse_supervisor_decision(payload)

    assert isinstance(decision, ReleaseFinalizationDecision)
    assert decision.outcome == ReleaseFinalizationOutcome.PR_PREPARATION


def test_parse_release_finalization_decision_stop_missing_policy() -> None:
    payload = {
        **BASE,
        "decision_type": SupervisorDecisionType.RELEASE_FINALIZATION,
        "risk_level": DecisionRiskLevel.HIGH,
        "policy_basis": ReleaseFinalizationPolicyName.STOP_MISSING_POLICY_OR_CREDENTIALS,
        "selected_action": ReleaseFinalizationPolicyName.STOP_MISSING_POLICY_OR_CREDENTIALS,
        "outcome": ReleaseFinalizationOutcome.STOP_MISSING_POLICY_OR_CREDENTIALS,
        "fallback_plan": "Stop and request explicit release finalization policy.",
        "validators_to_rerun": ["finalization_policy_presence"],
        "missing_policy": True,
        "outcome_references": ["runs/release/release_summary.json"],
    }

    decision = parse_supervisor_decision(payload)

    assert isinstance(decision, ReleaseFinalizationDecision)
    assert decision.missing_policy is True


def test_parse_release_finalization_decision_stop_missing_credentials() -> None:
    payload = {
        **BASE,
        "decision_type": SupervisorDecisionType.RELEASE_FINALIZATION,
        "risk_level": DecisionRiskLevel.HIGH,
        "policy_basis": ReleaseFinalizationPolicyName.STOP_MISSING_POLICY_OR_CREDENTIALS,
        "selected_action": ReleaseFinalizationPolicyName.STOP_MISSING_POLICY_OR_CREDENTIALS,
        "outcome": ReleaseFinalizationOutcome.STOP_MISSING_POLICY_OR_CREDENTIALS,
        "fallback_plan": "Stop and request the missing credentials before retry.",
        "validators_to_rerun": ["credentials_check"],
        "missing_credentials": True,
        "missing_credential_env_vars": ["GIT_REMOTE_TOKEN"],
        "outcome_references": ["runs/release/release_summary.json"],
    }

    decision = parse_supervisor_decision(payload)

    assert isinstance(decision, ReleaseFinalizationDecision)
    assert decision.missing_credentials is True


def test_parse_legacy_execution_strategy_decision_adds_validators_migration_default() -> None:
    payload = {
        **BASE,
        "decision_type": SupervisorDecisionType.EXECUTION_STRATEGY,
        "risk_level": DecisionRiskLevel.MODERATE,
        "selected_action": ExecutionStrategyAction.ONE_SHOT,
        "outcome": ExecutionStrategyOutcome.PROCEED_ONE_SHOT,
        "fallback_plan": "Decompose into sequential contracts if one-shot verification fails.",
    }

    decision = parse_supervisor_decision(payload)

    assert isinstance(decision, ExecutionStrategyDecision)
    assert decision.validators_to_rerun == [LEGACY_VALIDATORS_UNSPECIFIED]


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


def test_release_finalization_rejects_invalid_policy_action_outcome_combination() -> None:
    with pytest.raises(ValidationError, match="selected_action must match outcome"):
        ReleaseFinalizationDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.RELEASE_FINALIZATION,
                "risk_level": DecisionRiskLevel.MODERATE,
                "policy_basis": ReleaseFinalizationPolicyName.PUSH_FEATURE,
                "selected_action": ReleaseFinalizationPolicyName.PUSH_FEATURE,
                "outcome": ReleaseFinalizationOutcome.LOCAL_MERGE,
                "fallback_plan": "Stop and escalate if policy-action mismatch is detected.",
                "validators_to_rerun": ["finalization_gate"],
                "outcome_references": ["runs/release/release_summary.json"],
            }
        )

    with pytest.raises(ValidationError, match="policy_basis must match selected_action"):
        ReleaseFinalizationDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.RELEASE_FINALIZATION,
                "risk_level": DecisionRiskLevel.MODERATE,
                "policy_basis": ReleaseFinalizationPolicyName.LOCAL_MERGE,
                "selected_action": ReleaseFinalizationPolicyName.PUSH_FEATURE,
                "outcome": ReleaseFinalizationOutcome.PUSH_FEATURE,
                "fallback_plan": "Stop and escalate if policy-action mismatch is detected.",
                "validators_to_rerun": ["finalization_gate"],
                "outcome_references": ["runs/release/release_summary.json"],
            }
        )

    with pytest.raises(ValidationError, match="requires missing_policy or missing_credentials"):
        ReleaseFinalizationDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.RELEASE_FINALIZATION,
                "risk_level": DecisionRiskLevel.HIGH,
                "policy_basis": ReleaseFinalizationPolicyName.STOP_MISSING_POLICY_OR_CREDENTIALS,
                "selected_action": ReleaseFinalizationPolicyName.STOP_MISSING_POLICY_OR_CREDENTIALS,
                "outcome": ReleaseFinalizationOutcome.STOP_MISSING_POLICY_OR_CREDENTIALS,
                "fallback_plan": "Stop and request missing policy or credentials.",
                "validators_to_rerun": ["finalization_policy_presence"],
                "outcome_references": ["runs/release/release_summary.json"],
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

    with pytest.raises(ValidationError, match="Field required"):
        ExecutionStrategyDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.EXECUTION_STRATEGY,
                "risk_level": DecisionRiskLevel.MODERATE,
                "selected_action": ExecutionStrategyAction.REPLAN,
                "outcome": ExecutionStrategyOutcome.REPLAN,
                "fallback_plan": "Escalate to replanning.",
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


def test_parse_feature_review_finding_classification_decision() -> None:
    payload = {
        **BASE,
        "decision_type": SupervisorDecisionType.FEATURE_REVIEW_FINDING_CLASSIFICATION,
        "finding_id": "fr-321",
        "classification": FeatureReviewFindingClassification.SOFT_FINDING,
        "selected_action": FeatureReviewFindingAction.ACCEPT,
        "outcome": FeatureReviewFindingOutcome.CONTINUE,
        "fallback_plan": "Re-open as repair if related verification regresses.",
        "validators_to_rerun": ["review_findings_schema", "release_review_gate"],
        "evidence_paths": ["runs/release/release_review.md"],
    }

    decision = parse_supervisor_decision(payload)

    assert isinstance(decision, FeatureReviewFindingClassificationDecision)
    assert decision.decision_type == SupervisorDecisionType.FEATURE_REVIEW_FINDING_CLASSIFICATION
    assert decision.classification == FeatureReviewFindingClassification.SOFT_FINDING
    assert decision.selected_action == FeatureReviewFindingAction.ACCEPT


def test_parse_legacy_feature_review_finding_classification_decision_adds_validators_migration_default() -> None:
    payload = {
        **BASE,
        "decision_type": SupervisorDecisionType.FEATURE_REVIEW_FINDING_CLASSIFICATION,
        "finding_id": "fr-legacy-001",
        "classification": FeatureReviewFindingClassification.BLOCKER,
        "selected_action": FeatureReviewFindingAction.REPAIR,
        "outcome": FeatureReviewFindingOutcome.CONTINUE,
        "fallback_plan": "Escalate if bounded repair cannot resolve the blocker safely.",
    }

    decision = parse_supervisor_decision(payload)

    assert isinstance(decision, FeatureReviewFindingClassificationDecision)
    assert decision.validators_to_rerun == [LEGACY_VALIDATORS_UNSPECIFIED]


def test_feature_review_finding_classification_requires_expected_action_outcome_mapping() -> None:
    with pytest.raises(ValidationError, match="repair or accept requires continue outcome"):
        FeatureReviewFindingClassificationDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.FEATURE_REVIEW_FINDING_CLASSIFICATION,
                "finding_id": "fr-321",
                "classification": FeatureReviewFindingClassification.BLOCKER,
                "selected_action": FeatureReviewFindingAction.REPAIR,
                "outcome": FeatureReviewFindingOutcome.STOP,
                "fallback_plan": "Escalate to hard stop if repair cannot be scoped safely.",
                "validators_to_rerun": ["verification"],
            }
        )

    with pytest.raises(ValidationError, match="defer requires stop outcome"):
        FeatureReviewFindingClassificationDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.FEATURE_REVIEW_FINDING_CLASSIFICATION,
                "finding_id": "fr-321",
                "classification": FeatureReviewFindingClassification.BACKLOG_FOLLOW_UP,
                "selected_action": FeatureReviewFindingAction.DEFER,
                "outcome": FeatureReviewFindingOutcome.CONTINUE,
                "fallback_plan": "Track follow-up in backlog before next cycle.",
                "validators_to_rerun": ["review_findings_schema"],
            }
        )


def test_feature_review_finding_classification_non_blocking_accept_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="requires evidence_paths"):
        FeatureReviewFindingClassificationDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.FEATURE_REVIEW_FINDING_CLASSIFICATION,
                "finding_id": "fr-321",
                "classification": FeatureReviewFindingClassification.SOFT_FINDING,
                "selected_action": FeatureReviewFindingAction.ACCEPT,
                "outcome": FeatureReviewFindingOutcome.CONTINUE,
                "fallback_plan": "Re-open if related verification regresses.",
                "validators_to_rerun": ["review_findings_schema", "release_review_gate"],
                "evidence_paths": [],
            }
        )

    with pytest.raises(ValidationError, match="duplicate classification must not use accept action"):
        FeatureReviewFindingClassificationDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.FEATURE_REVIEW_FINDING_CLASSIFICATION,
                "finding_id": "fr-321",
                "classification": FeatureReviewFindingClassification.DUPLICATE,
                "selected_action": FeatureReviewFindingAction.ACCEPT,
                "outcome": FeatureReviewFindingOutcome.CONTINUE,
                "fallback_plan": "Re-open if duplicate linkage cannot be verified.",
                "validators_to_rerun": ["review_findings_schema", "release_review_gate"],
                "evidence_paths": ["runs/release/feature_review.json"],
            }
        )

    with pytest.raises(ValidationError, match="must not use accept action"):
        FeatureReviewFindingClassificationDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.FEATURE_REVIEW_FINDING_CLASSIFICATION,
                "finding_id": "fr-321",
                "classification": FeatureReviewFindingClassification.BLOCKER,
                "selected_action": FeatureReviewFindingAction.ACCEPT,
                "outcome": FeatureReviewFindingOutcome.CONTINUE,
                "fallback_plan": "Escalate to stop if blocker cannot be repaired safely.",
                "validators_to_rerun": ["verification"],
                "evidence_paths": ["runs/release/release_review.md"],
            }
        )


def test_feature_review_finding_classification_requires_validators_to_rerun_field() -> None:
    with pytest.raises(ValidationError, match="Field required"):
        FeatureReviewFindingClassificationDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.FEATURE_REVIEW_FINDING_CLASSIFICATION,
                "finding_id": "fr-321",
                "classification": FeatureReviewFindingClassification.BLOCKER,
                "selected_action": FeatureReviewFindingAction.REPAIR,
                "outcome": FeatureReviewFindingOutcome.CONTINUE,
                "fallback_plan": "Escalate if repair cannot be scoped safely.",
            }
        )


def test_parse_final_review_finding_adjudication_decision_all_classifications() -> None:
    classification_cases = [
        (
            FinalReviewFindingAdjudicationClassification.BLOCKER,
            FinalReviewFindingAdjudicationAction.REPAIR,
            FinalReviewFindingAdjudicationOutcome.CONTINUE,
        ),
        (
            FinalReviewFindingAdjudicationClassification.ACCEPTED_RISK,
            FinalReviewFindingAdjudicationAction.ACCEPT,
            FinalReviewFindingAdjudicationOutcome.CONTINUE,
        ),
        (
            FinalReviewFindingAdjudicationClassification.BACKLOG_FOLLOW_UP,
            FinalReviewFindingAdjudicationAction.DEFER,
            FinalReviewFindingAdjudicationOutcome.STOP,
        ),
        (
            FinalReviewFindingAdjudicationClassification.DUPLICATE,
            FinalReviewFindingAdjudicationAction.DEFER,
            FinalReviewFindingAdjudicationOutcome.STOP,
        ),
        (
            FinalReviewFindingAdjudicationClassification.FALSE_POSITIVE,
            FinalReviewFindingAdjudicationAction.ACCEPT,
            FinalReviewFindingAdjudicationOutcome.CONTINUE,
        ),
        (
            FinalReviewFindingAdjudicationClassification.SCOPE_EXPANSION,
            FinalReviewFindingAdjudicationAction.DEFER,
            FinalReviewFindingAdjudicationOutcome.STOP,
        ),
    ]

    for index, (classification, selected_action, outcome) in enumerate(classification_cases):
        payload = {
            **BASE,
            "decision_id": f"final-adjudication-{index}",
            "decision_type": SupervisorDecisionType.FINAL_REVIEW_FINDING_ADJUDICATION,
            "finding_id": f"finding-{index}",
            "classification": classification,
            "selected_action": selected_action,
            "outcome": outcome,
            "rationale": f"classification={classification.value}",
            "evidence_paths": [f"runs/release/final_review/finding_{index}.md"],
            "fallback_plan": "Re-enter bounded repair wave if adjudication evidence changes.",
            "validators_to_rerun": ["final_integration_verification", "release_review_gate"],
        }

        decision = parse_supervisor_decision(payload)
        assert isinstance(decision, FinalReviewFindingAdjudicationDecision)
        assert decision.classification == classification


def test_final_review_finding_adjudication_requires_rationale_evidence_fallback_and_validators() -> None:
    missing_rationale_payload = {
        **BASE,
        "decision_type": SupervisorDecisionType.FINAL_REVIEW_FINDING_ADJUDICATION,
        "finding_id": "finding-missing-rationale",
        "classification": FinalReviewFindingAdjudicationClassification.ACCEPTED_RISK,
        "selected_action": FinalReviewFindingAdjudicationAction.ACCEPT,
        "outcome": FinalReviewFindingAdjudicationOutcome.CONTINUE,
        "evidence_paths": ["runs/release/final_review/finding.md"],
        "fallback_plan": "Retry bounded repair.",
        "validators_to_rerun": ["final_integration_verification"],
    }
    missing_rationale_payload.pop("rationale", None)

    with pytest.raises(ValidationError, match="Field required"):
        FinalReviewFindingAdjudicationDecision.model_validate(missing_rationale_payload)

    with pytest.raises(ValidationError, match="evidence_paths must not be empty"):
        FinalReviewFindingAdjudicationDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.FINAL_REVIEW_FINDING_ADJUDICATION,
                "finding_id": "finding-missing-evidence",
                "classification": FinalReviewFindingAdjudicationClassification.ACCEPTED_RISK,
                "selected_action": FinalReviewFindingAdjudicationAction.ACCEPT,
                "outcome": FinalReviewFindingAdjudicationOutcome.CONTINUE,
                "fallback_plan": "Retry bounded repair.",
                "validators_to_rerun": ["final_integration_verification"],
                "evidence_paths": [],
            }
        )

    with pytest.raises(ValidationError, match="Field required"):
        FinalReviewFindingAdjudicationDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.FINAL_REVIEW_FINDING_ADJUDICATION,
                "finding_id": "finding-missing-fallback",
                "classification": FinalReviewFindingAdjudicationClassification.ACCEPTED_RISK,
                "selected_action": FinalReviewFindingAdjudicationAction.ACCEPT,
                "outcome": FinalReviewFindingAdjudicationOutcome.CONTINUE,
                "evidence_paths": ["runs/release/final_review/finding.md"],
                "validators_to_rerun": ["final_integration_verification"],
            }
        )

    with pytest.raises(ValidationError, match="validators to rerun must not be empty"):
        FinalReviewFindingAdjudicationDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.FINAL_REVIEW_FINDING_ADJUDICATION,
                "finding_id": "finding-empty-validators",
                "classification": FinalReviewFindingAdjudicationClassification.ACCEPTED_RISK,
                "selected_action": FinalReviewFindingAdjudicationAction.ACCEPT,
                "outcome": FinalReviewFindingAdjudicationOutcome.CONTINUE,
                "evidence_paths": ["runs/release/final_review/finding.md"],
                "fallback_plan": "Retry bounded repair.",
                "validators_to_rerun": [],
            }
        )


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


def test_parse_legacy_model_output_normalization_decision_requires_explicit_rerun_validators() -> None:
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
        "normalized_artifact_path": "runs/release/feature_review.normalized.json",
    }

    with pytest.raises(ValidationError, match="requires explicit validators_to_rerun"):
        parse_supervisor_decision(payload)


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

    with pytest.raises(ValidationError, match="validators to rerun must not be empty"):
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


def test_model_output_normalization_refused_allows_empty_validation_errors_but_requires_rerun_validators() -> None:
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
            "validators_to_rerun": ["review_findings_schema"],
            "refusal_reason": "Insufficient confidence in safe normalization.",
        }
    )

    assert decision.outcome == ModelOutputNormalizationOutcome.REFUSED_AND_STOP
    assert decision.validators_to_rerun == ["review_findings_schema"]

    with pytest.raises(ValidationError, match="validators to rerun must not be empty"):
        ModelOutputNormalizationDecision.model_validate(
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


def test_parse_scope_risk_budget_policy_decision() -> None:
    payload = {
        **BASE,
        "decision_type": SupervisorDecisionType.SCOPE_RISK_BUDGET_POLICY,
        "classification": ScopeRiskClassification.COHESIVE,
        "selected_action": ScopeRiskAction.ACCEPT_WITH_GUARDS,
        "outcome": ScopeRiskOutcome.ACCEPTED_WITH_GUARDS,
        "fallback_plan": "Split and rerun if verification detects semantic drift.",
        "validators_to_rerun": ["changed_files", "diff_size", "verification"],
        "configured_changed_files_limit": 8,
        "actual_changed_files": 12,
        "configured_diff_size_limit": 500,
        "actual_diff_size": 740,
        "affected_scope": ScopeRiskAffectedScope.TASK,
        "affected_task_id": "soft-scope-budget-policy-0001",
        "evidence_paths": ["runs/release/changed_files.txt", "runs/release/git_diff.patch"],
    }

    decision = parse_supervisor_decision(payload)

    assert isinstance(decision, ScopeRiskBudgetPolicyDecision)
    assert decision.classification == ScopeRiskClassification.COHESIVE
    assert decision.selected_action == ScopeRiskAction.ACCEPT_WITH_GUARDS


def test_scope_risk_budget_policy_requires_non_empty_required_fields() -> None:
    with pytest.raises(ValidationError, match="evidence_paths must not be empty"):
        ScopeRiskBudgetPolicyDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.SCOPE_RISK_BUDGET_POLICY,
                "classification": ScopeRiskClassification.COHESIVE,
                "selected_action": ScopeRiskAction.ACCEPT_WITH_GUARDS,
                "outcome": ScopeRiskOutcome.ACCEPTED_WITH_GUARDS,
                "fallback_plan": "Split if verification regresses.",
                "validators_to_rerun": ["verification"],
                "configured_changed_files_limit": 8,
                "actual_changed_files": 12,
                "configured_diff_size_limit": 500,
                "actual_diff_size": 740,
                "affected_scope": ScopeRiskAffectedScope.TASK,
                "affected_task_id": "soft-scope-budget-policy-0001",
                "evidence_paths": [],
            }
        )

    with pytest.raises(ValidationError, match="validators to rerun must not be empty"):
        ScopeRiskBudgetPolicyDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.SCOPE_RISK_BUDGET_POLICY,
                "classification": ScopeRiskClassification.COHESIVE,
                "selected_action": ScopeRiskAction.ACCEPT_WITH_GUARDS,
                "outcome": ScopeRiskOutcome.ACCEPTED_WITH_GUARDS,
                "fallback_plan": "Split if verification regresses.",
                "validators_to_rerun": [],
                "configured_changed_files_limit": 8,
                "actual_changed_files": 12,
                "configured_diff_size_limit": 500,
                "actual_diff_size": 740,
                "affected_scope": ScopeRiskAffectedScope.TASK,
                "affected_task_id": "soft-scope-budget-policy-0001",
                "evidence_paths": ["runs/release/changed_files.txt"],
            }
        )


@pytest.mark.parametrize("evidence_path", ["/tmp/changed_files.txt", "../changed_files.txt"])
def test_scope_risk_budget_policy_requires_release_relative_evidence_paths(evidence_path: str) -> None:
    with pytest.raises(ValidationError, match="scope-risk evidence_paths"):
        ScopeRiskBudgetPolicyDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.SCOPE_RISK_BUDGET_POLICY,
                "classification": ScopeRiskClassification.MECHANICAL,
                "selected_action": ScopeRiskAction.REPLAN,
                "outcome": ScopeRiskOutcome.REPLAN_AND_RETRY,
                "fallback_plan": "Split if verification regresses.",
                "validators_to_rerun": ["verification"],
                "configured_changed_files_limit": 8,
                "actual_changed_files": 12,
                "configured_diff_size_limit": 500,
                "actual_diff_size": 740,
                "affected_scope": ScopeRiskAffectedScope.TASK,
                "affected_task_id": "soft-scope-budget-policy-0001",
                "evidence_paths": [evidence_path],
            }
        )


def test_scope_risk_budget_policy_blocks_soft_acceptance_for_hard_safety_findings() -> None:
    with pytest.raises(ValidationError, match="must not include hard_safety_findings"):
        ScopeRiskBudgetPolicyDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.SCOPE_RISK_BUDGET_POLICY,
                "classification": ScopeRiskClassification.MECHANICAL,
                "selected_action": ScopeRiskAction.ACCEPT_WITH_GUARDS,
                "outcome": ScopeRiskOutcome.ACCEPTED_WITH_GUARDS,
                "fallback_plan": "Stop if hard gates are violated.",
                "validators_to_rerun": ["verification"],
                "configured_changed_files_limit": 8,
                "actual_changed_files": 20,
                "configured_diff_size_limit": 500,
                "actual_diff_size": 2000,
                "affected_scope": ScopeRiskAffectedScope.RELEASE,
                "evidence_paths": ["runs/release/changed_files.txt"],
                "hard_safety_findings": ["forbidden path touched: migrations/001.sql"],
            }
        )


def test_scope_risk_budget_policy_allows_mechanical_acceptance_without_hard_safety_findings() -> None:
    decision = ScopeRiskBudgetPolicyDecision.model_validate(
        {
            **BASE,
            "decision_type": SupervisorDecisionType.SCOPE_RISK_BUDGET_POLICY,
            "classification": ScopeRiskClassification.MECHANICAL,
            "selected_action": ScopeRiskAction.ACCEPT_WITH_GUARDS,
            "outcome": ScopeRiskOutcome.ACCEPTED_WITH_GUARDS,
            "fallback_plan": "Escalate to split if verification regresses.",
            "validators_to_rerun": ["verification", "diff_size"],
            "configured_changed_files_limit": 8,
            "actual_changed_files": 19,
            "configured_diff_size_limit": 500,
            "actual_diff_size": 1500,
            "affected_scope": ScopeRiskAffectedScope.TASK,
            "affected_task_id": "soft-scope-budget-policy-0001",
            "evidence_paths": ["runs/release/changed_files.txt"],
        }
    )

    assert decision.selected_action == ScopeRiskAction.ACCEPT_WITH_GUARDS
    assert decision.outcome == ScopeRiskOutcome.ACCEPTED_WITH_GUARDS


def test_scope_risk_budget_policy_rejects_risky_accept_with_guards_classification() -> None:
    with pytest.raises(ValidationError, match="accept_with_guards requires mechanical or cohesive classification"):
        ScopeRiskBudgetPolicyDecision.model_validate(
            {
                **BASE,
                "decision_type": SupervisorDecisionType.SCOPE_RISK_BUDGET_POLICY,
                "classification": ScopeRiskClassification.RISKY,
                "selected_action": ScopeRiskAction.ACCEPT_WITH_GUARDS,
                "outcome": ScopeRiskOutcome.ACCEPTED_WITH_GUARDS,
                "fallback_plan": "Escalate to hard stop.",
                "validators_to_rerun": ["verification"],
                "configured_changed_files_limit": 8,
                "actual_changed_files": 12,
                "configured_diff_size_limit": 500,
                "actual_diff_size": 740,
                "affected_scope": ScopeRiskAffectedScope.TASK,
                "affected_task_id": "soft-scope-budget-policy-0001",
                "evidence_paths": ["runs/release/changed_files.txt"],
            }
        )


@pytest.mark.parametrize(
    ("selected_action", "outcome"),
    [
        (ScopeRiskAction.SPLIT_TASK, ScopeRiskOutcome.SPLIT_AND_RETRY),
        (ScopeRiskAction.REPLAN, ScopeRiskOutcome.REPLAN_AND_RETRY),
    ],
)
def test_parse_scope_risk_budget_policy_split_and_replan_decisions(
    selected_action: ScopeRiskAction,
    outcome: ScopeRiskOutcome,
) -> None:
    payload = {
        **BASE,
        "decision_type": SupervisorDecisionType.SCOPE_RISK_BUDGET_POLICY,
        "classification": ScopeRiskClassification.COHESIVE,
        "selected_action": selected_action,
        "outcome": outcome,
        "fallback_plan": "Retry via bounded contract decomposition.",
        "validators_to_rerun": ["changed_files", "verification"],
        "configured_changed_files_limit": 8,
        "actual_changed_files": 12,
        "configured_diff_size_limit": 500,
        "actual_diff_size": 740,
        "affected_scope": ScopeRiskAffectedScope.TASK,
        "affected_task_id": "soft-scope-budget-policy-0001",
        "evidence_paths": ["runs/release/changed_files.txt", "runs/release/git_diff.patch"],
    }

    decision = parse_supervisor_decision(payload)

    assert isinstance(decision, ScopeRiskBudgetPolicyDecision)
    assert decision.selected_action == selected_action
    assert decision.outcome == outcome


def test_effective_validators_to_rerun_filters_legacy_sentinel() -> None:
    assert effective_validators_to_rerun([LEGACY_VALIDATORS_UNSPECIFIED]) == []
    assert effective_validators_to_rerun(["verification"]) == ["verification"]


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
