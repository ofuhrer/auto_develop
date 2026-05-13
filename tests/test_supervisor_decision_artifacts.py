from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentic_devloop.evidence import supervisor_decisions_artifacts_dir
from agentic_devloop.supervisor_decisions import (
    DecisionRiskLevel,
    ExecutionStrategyAction,
    ExecutionStrategyDecision,
    ExecutionStrategyOutcome,
    FeatureReviewFindingAction,
    FeatureReviewFindingClassification,
    FeatureReviewFindingClassificationDecision,
    FeatureReviewFindingOutcome,
    LEGACY_VALIDATORS_UNSPECIFIED,
    ModelOutputNormalizationAction,
    ModelOutputNormalizationDecision,
    ModelOutputNormalizationOutcome,
    ReleaseSchedulingAction,
    ReleaseSchedulingDecision,
    SCHEMA_VERSION_V1,
    SchedulingOutcome,
    ScopeRiskAction,
    ScopeRiskAffectedScope,
    ScopeRiskBudgetPolicyDecision,
    ScopeRiskClassification,
    ScopeRiskOutcome,
    SupervisorDecisionType,
    build_model_output_normalization_decision,
    load_supervisor_decision_artifact,
    supervisor_decision_artifact_path,
    write_supervisor_decision_artifact,
)
from agentic_devloop.models import ModelOutputNormalizationActionPayload, PlannerAdmissionRepairActionPayload


def _decision(*, evidence_paths: list[Path]) -> ReleaseSchedulingDecision:
    return ReleaseSchedulingDecision.model_validate(
        {
            "schema_version": SCHEMA_VERSION_V1,
            "decision_id": "schedule-001",
            "release_id": "supervisor-decision-records",
            "decided_at": datetime(2026, 5, 13, 8, 0, 0),
            "decided_by": "supervisor-agent",
            "rationale": "Serialized overlap findings indicate sequential execution.",
            "evidence_paths": evidence_paths,
            "decision_type": SupervisorDecisionType.RELEASE_SCHEDULING,
            "risk_level": DecisionRiskLevel.MODERATE,
            "overlap_findings": ["src/agentic_devloop/release.py"],
            "selected_action": ReleaseSchedulingAction.SEQUENTIAL,
            "outcome": SchedulingOutcome.PROCEED_SEQUENTIAL,
            "fallback_plan": "Rerun overlap analysis before resuming parallel execution.",
            "validators_to_rerun": ["overlap_report", "verification"],
            "staleness_inputs": {
                "execution_mode": "parallel",
                "selected_task_ids": ["demo-0001"],
                "selected_contract_paths": [str(Path("contract.yaml"))],
                "overlap_report_sha256": "abc123",
                "base_branch_head_commit": "deadbeef",
                "release_inputs_sha256": "f00d",
            },
        }
    )


def _execution_strategy_decision(*, evidence_paths: list[Path]) -> ExecutionStrategyDecision:
    return ExecutionStrategyDecision.model_validate(
        {
            "schema_version": SCHEMA_VERSION_V1,
            "decision_id": "strategy-001",
            "release_id": "supervisor-execution-strategy",
            "decided_at": datetime(2026, 5, 13, 8, 0, 0),
            "decided_by": "supervisor-agent",
            "rationale": "Cohesive implementation and shared verification indicate one-shot execution.",
            "evidence_paths": evidence_paths,
            "decision_type": SupervisorDecisionType.EXECUTION_STRATEGY,
            "risk_level": DecisionRiskLevel.MODERATE,
            "selected_action": ExecutionStrategyAction.ONE_SHOT,
            "outcome": ExecutionStrategyOutcome.PROCEED_ONE_SHOT,
            "fallback_plan": "Decompose to sequential contracts if one-shot verification fails.",
            "validators_to_rerun": ["contract_plan", "verification"],
        }
    )


def _model_output_normalization_decision(*, evidence_paths: list[Path]) -> ModelOutputNormalizationDecision:
    return ModelOutputNormalizationDecision.model_validate(
        {
            "schema_version": SCHEMA_VERSION_V1,
            "decision_id": "normalization-001",
            "release_id": "model-output-normalization",
            "decided_at": datetime(2026, 5, 13, 8, 0, 0),
            "decided_by": "supervisor-agent",
            "rationale": "Raw output is semantically useful and can be safely normalized.",
            "evidence_paths": evidence_paths,
            "decision_type": SupervisorDecisionType.MODEL_OUTPUT_NORMALIZATION,
            "risk_level": DecisionRiskLevel.MODERATE,
            "raw_artifact_paths": [Path("feature_review.raw.json")],
            "validation_errors": [
                {
                    "field": "findings[0].evidence_paths",
                    "message": "Field required",
                    "error_type": "missing",
                }
            ],
            "selected_action": ModelOutputNormalizationAction.APPLY_NORMALIZATION,
            "outcome": ModelOutputNormalizationOutcome.NORMALIZED_AND_RETRY,
            "fallback_plan": "Refuse and stop if normalized output fails validation.",
            "validators_to_rerun": ["review_findings_schema", "release_review_gate"],
            "normalized_artifact_path": Path("feature_review.normalized.json"),
        }
    )


def _feature_review_finding_classification_decision(
    *, evidence_paths: list[Path]
) -> FeatureReviewFindingClassificationDecision:
    return FeatureReviewFindingClassificationDecision.model_validate(
        {
            "schema_version": SCHEMA_VERSION_V1,
            "decision_id": "finding-classification-001",
            "release_id": "review-loop-convergence-policy",
            "decided_at": datetime(2026, 5, 13, 8, 0, 0),
            "decided_by": "supervisor-agent",
            "rationale": "Finding is a soft issue and accepted with bounded follow-up checks.",
            "evidence_paths": evidence_paths,
            "decision_type": SupervisorDecisionType.FEATURE_REVIEW_FINDING_CLASSIFICATION,
            "finding_id": "fr-321",
            "classification": FeatureReviewFindingClassification.SOFT_FINDING,
            "selected_action": FeatureReviewFindingAction.ACCEPT,
            "outcome": FeatureReviewFindingOutcome.CONTINUE,
            "fallback_plan": "Re-open as repair if related verification regresses.",
            "validators_to_rerun": ["review_findings_schema", "release_review_gate"],
        }
    )


def _scope_risk_budget_policy_decision(*, evidence_paths: list[Path]) -> ScopeRiskBudgetPolicyDecision:
    return ScopeRiskBudgetPolicyDecision.model_validate(
        {
            "schema_version": SCHEMA_VERSION_V1,
            "decision_id": "scope-risk-001",
            "release_id": "soft-scope-budget-policy",
            "decided_at": datetime(2026, 5, 13, 10, 0, 0, tzinfo=UTC),
            "decided_by": "supervisor-agent",
            "rationale": "Broad edits are cohesive and mechanically safe with stronger verification reruns.",
            "evidence_paths": evidence_paths,
            "decision_type": SupervisorDecisionType.SCOPE_RISK_BUDGET_POLICY,
            "classification": ScopeRiskClassification.COHESIVE,
            "selected_action": ScopeRiskAction.ACCEPT_WITH_GUARDS,
            "outcome": ScopeRiskOutcome.ACCEPTED_WITH_GUARDS,
            "fallback_plan": "Split and rerun if verification identifies semantic drift.",
            "validators_to_rerun": ["changed_files", "diff_size", "verification"],
            "configured_changed_files_limit": 8,
            "actual_changed_files": 14,
            "configured_diff_size_limit": 500,
            "actual_diff_size": 910,
            "affected_scope": ScopeRiskAffectedScope.TASK,
            "affected_task_id": "soft-scope-budget-policy-0001",
        }
    )


def test_supervisor_decision_artifact_path_is_deterministic(tmp_path: Path) -> None:
    path = supervisor_decision_artifact_path(
        release_bundle_path=tmp_path,
        decision_type=SupervisorDecisionType.REPAIR_LOOP_CONTINUATION,
        decision_id="repair-003",
    )

    assert path == tmp_path / "supervisor_decisions" / "repair_loop_continuation__repair-003.json"
    assert supervisor_decisions_artifacts_dir(tmp_path) == tmp_path / "supervisor_decisions"


@pytest.mark.parametrize("decision_id", ["../escape", "nested/path", r"nested\path", "safe..but-bad"])
def test_supervisor_decision_artifact_path_rejects_path_like_decision_ids(
    tmp_path: Path, decision_id: str
) -> None:
    with pytest.raises(ValueError, match="path separators"):
        supervisor_decision_artifact_path(
            release_bundle_path=tmp_path,
            decision_type=SupervisorDecisionType.REPAIR_LOOP_CONTINUATION,
            decision_id=decision_id,
        )


def test_supervisor_decision_artifact_path_sanitizes_filename_token(tmp_path: Path) -> None:
    path = supervisor_decision_artifact_path(
        release_bundle_path=tmp_path,
        decision_type=SupervisorDecisionType.REPAIR_LOOP_CONTINUATION,
        decision_id="repair 003:retry",
    )

    assert path == tmp_path / "supervisor_decisions" / "repair_loop_continuation__repair_003_retry.json"


def test_write_and_load_supervisor_decision_artifact_round_trip(tmp_path: Path) -> None:
    evidence_file = tmp_path / "verification.log"
    evidence_file.write_text("ok\n", encoding="utf-8")
    decision = _decision(evidence_paths=[evidence_file])

    artifact_path = write_supervisor_decision_artifact(
        release_bundle_path=tmp_path,
        decision=decision,
    )
    loaded = load_supervisor_decision_artifact(artifact_path)

    assert artifact_path.exists()
    assert loaded == decision


def test_load_supervisor_decision_artifact_accepts_bundle_relative_evidence_path(
    tmp_path: Path,
) -> None:
    evidence_file = tmp_path / "changed_files.txt"
    evidence_file.write_text("src/agentic_devloop/release.py\n", encoding="utf-8")
    decision = _decision(evidence_paths=[Path("changed_files.txt")])

    artifact_path = write_supervisor_decision_artifact(
        release_bundle_path=tmp_path,
        decision=decision,
    )
    loaded = load_supervisor_decision_artifact(artifact_path)

    assert loaded == decision


def test_write_and_load_execution_strategy_artifact_round_trip(tmp_path: Path) -> None:
    evidence_file = tmp_path / "strategy-evidence.log"
    evidence_file.write_text("selected one-shot\n", encoding="utf-8")
    decision = _execution_strategy_decision(evidence_paths=[evidence_file])

    artifact_path = write_supervisor_decision_artifact(
        release_bundle_path=tmp_path,
        decision=decision,
    )
    loaded = load_supervisor_decision_artifact(artifact_path)

    assert artifact_path.exists()
    assert loaded == decision


def test_load_legacy_execution_strategy_artifact_adds_validators_migration_default(
    tmp_path: Path,
) -> None:
    evidence_file = tmp_path / "strategy-evidence.log"
    evidence_file.write_text("selected one-shot\n", encoding="utf-8")
    artifact_path = tmp_path / "supervisor_decisions" / "legacy-execution-strategy.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION_V1,
                "decision_id": "legacy-strategy-001",
                "release_id": "supervisor-execution-strategy",
                "decided_at": "2026-05-13T08:00:00",
                "decided_by": "supervisor-agent",
                "rationale": "Legacy artifact predates explicit validator rerun storage.",
                "evidence_paths": ["strategy-evidence.log"],
                "decision_type": SupervisorDecisionType.EXECUTION_STRATEGY,
                "risk_level": DecisionRiskLevel.MODERATE,
                "selected_action": ExecutionStrategyAction.ONE_SHOT,
                "outcome": ExecutionStrategyOutcome.PROCEED_ONE_SHOT,
                "fallback_plan": "Decompose to sequential contracts if one-shot verification fails.",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="legacy supervisor decision artifact"):
        loaded = load_supervisor_decision_artifact(artifact_path)

    assert isinstance(loaded, ExecutionStrategyDecision)
    assert loaded.validators_to_rerun == [LEGACY_VALIDATORS_UNSPECIFIED]


def test_load_legacy_model_output_normalization_artifact_requires_explicit_rerun_validators(
    tmp_path: Path,
) -> None:
    evidence_file = tmp_path / "normalization-evidence.log"
    evidence_file.write_text("normalized output accepted\n", encoding="utf-8")
    raw_artifact = tmp_path / "feature_review.raw.json"
    raw_artifact.write_text("{}\n", encoding="utf-8")
    normalized_artifact = tmp_path / "feature_review.normalized.json"
    normalized_artifact.write_text("{}\n", encoding="utf-8")
    artifact_path = tmp_path / "supervisor_decisions" / "legacy-model-output-normalization.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION_V1,
                "decision_id": "legacy-normalization-001",
                "release_id": "model-output-normalization",
                "decided_at": "2026-05-13T08:00:00",
                "decided_by": "supervisor-agent",
                "rationale": "Legacy artifact predates explicit validator rerun storage.",
                "evidence_paths": ["normalization-evidence.log"],
                "decision_type": SupervisorDecisionType.MODEL_OUTPUT_NORMALIZATION,
                "risk_level": DecisionRiskLevel.MODERATE,
                "raw_artifact_paths": ["feature_review.raw.json"],
                "validation_errors": [
                    {
                        "field": "findings[0].evidence_paths",
                        "message": "Field required",
                        "error_type": "missing",
                    }
                ],
                "selected_action": ModelOutputNormalizationAction.APPLY_NORMALIZATION,
                "outcome": ModelOutputNormalizationOutcome.NORMALIZED_AND_RETRY,
                "fallback_plan": "Refuse and stop if normalized output still fails validation.",
                "normalized_artifact_path": "feature_review.normalized.json",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="legacy supervisor decision artifact"):
        with pytest.raises(ValidationError, match="requires explicit validators_to_rerun"):
            load_supervisor_decision_artifact(artifact_path)


def test_load_legacy_feature_review_finding_classification_artifact_adds_validators_migration_default(
    tmp_path: Path,
) -> None:
    evidence_file = tmp_path / "finding-classification-evidence.log"
    evidence_file.write_text("legacy finding classification\n", encoding="utf-8")
    artifact_path = tmp_path / "supervisor_decisions" / "legacy-feature-review-finding-classification.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION_V1,
                "decision_id": "legacy-finding-classification-001",
                "release_id": "review-loop-convergence-policy",
                "decided_at": "2026-05-13T08:00:00",
                "decided_by": "supervisor-agent",
                "rationale": "Legacy artifact predates explicit validator rerun storage.",
                "evidence_paths": ["finding-classification-evidence.log"],
                "decision_type": SupervisorDecisionType.FEATURE_REVIEW_FINDING_CLASSIFICATION,
                "finding_id": "fr-legacy-001",
                "classification": FeatureReviewFindingClassification.BACKLOG_FOLLOW_UP,
                "selected_action": FeatureReviewFindingAction.DEFER,
                "outcome": FeatureReviewFindingOutcome.STOP,
                "fallback_plan": "Track backlog follow-up and stop the current finding.",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="legacy supervisor decision artifact"):
        loaded = load_supervisor_decision_artifact(artifact_path)

    assert isinstance(loaded, FeatureReviewFindingClassificationDecision)
    assert loaded.validators_to_rerun == [LEGACY_VALIDATORS_UNSPECIFIED]


def test_write_and_load_model_output_normalization_artifact_round_trip(tmp_path: Path) -> None:
    evidence_file = tmp_path / "normalization-evidence.log"
    evidence_file.write_text("normalized output accepted\n", encoding="utf-8")
    raw_artifact = tmp_path / "feature_review.raw.json"
    raw_artifact.write_text("{}\n", encoding="utf-8")
    normalized_artifact = tmp_path / "feature_review.normalized.json"
    normalized_artifact.write_text("{}\n", encoding="utf-8")
    decision = _model_output_normalization_decision(
        evidence_paths=[evidence_file, raw_artifact, normalized_artifact]
    )

    artifact_path = write_supervisor_decision_artifact(
        release_bundle_path=tmp_path,
        decision=decision,
    )
    loaded = load_supervisor_decision_artifact(artifact_path)

    assert artifact_path.exists()
    assert loaded == decision


def test_write_and_load_model_output_normalization_applied_from_reusable_api(tmp_path: Path) -> None:
    evidence_file = tmp_path / "normalization-evidence.log"
    evidence_file.write_text("applied\n", encoding="utf-8")
    raw_artifact = tmp_path / "planner.raw.json"
    raw_artifact.write_text("{}\n", encoding="utf-8")
    normalized_artifact = tmp_path / "planner.normalized.json"
    normalized_artifact.write_text("{}\n", encoding="utf-8")

    action_payload = ModelOutputNormalizationActionPayload.model_validate(
        {
            "raw_artifact_paths": [Path("planner.raw.json")],
            "validation_errors": [
                {"field": "generated_contracts", "message": "Field required", "error_type": "missing"}
            ],
            "selected_action": "apply_normalization",
            "outcome": "normalized_and_retry",
            "rationale": "Raw planner output is useful but needs normalization.",
            "fallback_plan": "Refuse and stop if normalized output still fails strict validation.",
            "validators_to_rerun": ["contract_plan", "verification"],
            "normalized_artifact_path": Path("planner.normalized.json"),
        }
    )
    decision = build_model_output_normalization_decision(
        decision_id="normalization-applied-001",
        release_id="planner-normalization-generalization",
        decided_at=datetime(2026, 5, 13, 9, 0, 0),
        decided_by="supervisor-agent",
        risk_level=DecisionRiskLevel.MODERATE,
        evidence_paths=[Path("normalization-evidence.log")],
        action_payload=action_payload,
    )

    artifact_path = write_supervisor_decision_artifact(release_bundle_path=tmp_path, decision=decision)
    loaded = load_supervisor_decision_artifact(artifact_path)

    assert loaded == decision
    assert loaded.model_dump(mode="json")["outcome"] == "normalized_and_retry"


def test_write_and_load_model_output_normalization_refused_from_reusable_api(tmp_path: Path) -> None:
    evidence_file = tmp_path / "normalization-evidence.log"
    evidence_file.write_text("refused\n", encoding="utf-8")
    raw_artifact = tmp_path / "planner.raw.json"
    raw_artifact.write_text("{}\n", encoding="utf-8")

    action_payload = ModelOutputNormalizationActionPayload.model_validate(
        {
            "raw_artifact_paths": [Path("planner.raw.json")],
            "validation_errors": [{"field": "release_id", "message": "Invalid type", "error_type": "string_type"}],
            "selected_action": "refuse",
            "outcome": "refused_and_stop",
            "rationale": "Schema drift is unsafe to normalize under current contract boundaries.",
            "fallback_plan": "Stop and request a fresh planner run.",
            "validators_to_rerun": ["contract_plan"],
            "refusal_reason": "Unsafe normalization for contract semantics.",
        }
    )
    decision = build_model_output_normalization_decision(
        decision_id="normalization-refused-001",
        release_id="planner-normalization-generalization",
        decided_at=datetime(2026, 5, 13, 9, 5, 0),
        decided_by="supervisor-agent",
        risk_level=DecisionRiskLevel.HIGH,
        evidence_paths=[Path("normalization-evidence.log")],
        action_payload=action_payload,
    )

    artifact_path = write_supervisor_decision_artifact(release_bundle_path=tmp_path, decision=decision)
    loaded = load_supervisor_decision_artifact(artifact_path)

    assert loaded == decision
    assert loaded.model_dump(mode="json")["outcome"] == "refused_and_stop"


@pytest.mark.parametrize(
    "refusal_reason",
    [
        "Unsafe normalization would change contract intent.",
        "Unsafe normalization would invent hard evidence requirements.",
        "Unsafe normalization would weaken verification constraints.",
        "Unsafe normalization would violate allowed-file/forbidden-change policy boundaries.",
    ],
)
def test_model_output_normalization_refusal_persists_semantic_safety_categories(
    tmp_path: Path,
    refusal_reason: str,
) -> None:
    evidence_file = tmp_path / "normalization-evidence.log"
    evidence_file.write_text("refused\n", encoding="utf-8")
    raw_artifact = tmp_path / "planner.raw.json"
    raw_artifact.write_text("{}\n", encoding="utf-8")

    action_payload = ModelOutputNormalizationActionPayload.model_validate(
        {
            "raw_artifact_paths": [Path("planner.raw.json")],
            "validation_errors": [{"field": "generated_contracts", "message": "Invalid shape", "error_type": "mapping_type"}],
            "selected_action": "refuse",
            "outcome": "refused_and_stop",
            "rationale": "Refuse unsafe semantic rewrite.",
            "fallback_plan": "Stop and request a fresh planner run.",
            "validators_to_rerun": ["contract_plan"],
            "refusal_reason": refusal_reason,
        }
    )
    decision = build_model_output_normalization_decision(
        decision_id="normalization-refused-semantic-001",
        release_id="planner-normalization-generalization",
        decided_at=datetime(2026, 5, 13, 10, 0, 0),
        decided_by="supervisor-agent",
        risk_level=DecisionRiskLevel.HIGH,
        evidence_paths=[Path("normalization-evidence.log")],
        action_payload=action_payload,
    )

    artifact_path = write_supervisor_decision_artifact(release_bundle_path=tmp_path, decision=decision)
    loaded = load_supervisor_decision_artifact(artifact_path)

    assert loaded == decision
    assert loaded.refusal_reason == refusal_reason


def test_write_and_load_feature_review_finding_classification_artifact_round_trip(tmp_path: Path) -> None:
    evidence_file = tmp_path / "finding-classification-evidence.log"
    evidence_file.write_text("duplicate accepted with evidence\n", encoding="utf-8")
    decision = _feature_review_finding_classification_decision(evidence_paths=[evidence_file])

    artifact_path = write_supervisor_decision_artifact(
        release_bundle_path=tmp_path,
        decision=decision,
    )
    loaded = load_supervisor_decision_artifact(artifact_path)

    assert artifact_path.exists()
    assert loaded == decision


def test_scope_risk_budget_policy_artifact_serialization_and_round_trip(tmp_path: Path) -> None:
    changed_files = tmp_path / "changed_files.txt"
    changed_files.write_text("src/agentic_devloop/supervisor_decisions.py\n", encoding="utf-8")
    git_diff = tmp_path / "git_diff.patch"
    git_diff.write_text("diff --git a/x b/x\n", encoding="utf-8")
    decision = _scope_risk_budget_policy_decision(
        evidence_paths=[Path("changed_files.txt"), Path("git_diff.patch")]
    )

    artifact_path = write_supervisor_decision_artifact(
        release_bundle_path=tmp_path,
        decision=decision,
    )
    loaded = load_supervisor_decision_artifact(artifact_path)
    serialized = decision.model_dump(mode="json")

    assert artifact_path.exists()
    assert loaded == decision
    assert serialized["decision_type"] == "scope_risk_budget_policy"
    assert serialized["classification"] == "cohesive"
    assert serialized["selected_action"] == "accept_with_guards"
    assert serialized["configured_changed_files_limit"] == 8
    assert serialized["actual_changed_files"] == 14
    assert serialized["configured_diff_size_limit"] == 500
    assert serialized["actual_diff_size"] == 910
    assert serialized["affected_scope"] == "task"
    assert serialized["affected_task_id"] == "soft-scope-budget-policy-0001"
    assert serialized["rationale"]
    assert serialized["fallback_plan"]
    assert serialized["validators_to_rerun"]
    assert serialized["evidence_paths"]


def test_scope_risk_budget_policy_normalizes_naive_decided_at_to_utc() -> None:
    decision = ScopeRiskBudgetPolicyDecision.model_validate(
        {
            "schema_version": SCHEMA_VERSION_V1,
            "decision_id": "scope-risk-naive-001",
            "release_id": "soft-scope-budget-policy",
            "decided_at": datetime(2026, 5, 13, 10, 0, 0),
            "decided_by": "supervisor-agent",
            "rationale": "Naive decided_at must be normalized deterministically.",
            "evidence_paths": [Path("scope-risk.log")],
            "decision_type": SupervisorDecisionType.SCOPE_RISK_BUDGET_POLICY,
            "classification": ScopeRiskClassification.COHESIVE,
            "selected_action": ScopeRiskAction.ACCEPT_WITH_GUARDS,
            "outcome": ScopeRiskOutcome.ACCEPTED_WITH_GUARDS,
            "fallback_plan": "Split and retry if verification regresses.",
            "validators_to_rerun": ["verification"],
            "configured_changed_files_limit": 8,
            "actual_changed_files": 10,
            "configured_diff_size_limit": 500,
            "actual_diff_size": 650,
            "affected_scope": ScopeRiskAffectedScope.TASK,
            "affected_task_id": "soft-scope-budget-policy-0001",
            "hard_safety_findings": [],
        }
    )

    assert decision.decided_at.tzinfo is UTC


def test_load_supervisor_decision_artifact_fails_when_missing(tmp_path: Path) -> None:
    missing = tmp_path / "supervisor_decisions" / "missing.json"

    with pytest.raises(FileNotFoundError, match="does not exist"):
        load_supervisor_decision_artifact(missing)


def test_load_supervisor_decision_artifact_fails_on_invalid_payload(tmp_path: Path) -> None:
    artifact_path = tmp_path / "supervisor_decisions" / "invalid.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text('{"decision_type": "release_scheduling"}\n', encoding="utf-8")

    with pytest.raises(ValidationError):
        load_supervisor_decision_artifact(artifact_path)


def test_load_supervisor_decision_artifact_fails_for_missing_evidence_path(tmp_path: Path) -> None:
    decision = _decision(evidence_paths=[Path("missing-evidence.log")])
    artifact_path = write_supervisor_decision_artifact(
        release_bundle_path=tmp_path,
        decision=decision,
    )

    with pytest.raises(ValueError, match="missing evidence path"):
        load_supervisor_decision_artifact(artifact_path)


def test_load_supervisor_decision_artifact_rejects_relative_evidence_path_traversal(
    tmp_path: Path,
) -> None:
    escaped_evidence = tmp_path / "escape.log"
    escaped_evidence.write_text("outside bundle\n", encoding="utf-8")
    artifact_path = tmp_path / "supervisor_decisions" / "traversal.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION_V1,
                "decision_id": "traversal",
                "release_id": "supervisor-decision-records",
                "decided_at": "2026-05-13T08:00:00",
                "decided_by": "supervisor-agent",
                "rationale": "Traversal should be rejected.",
                "evidence_paths": ["../escape.log"],
                "decision_type": "release_scheduling",
                "risk_level": "moderate",
                "overlap_findings": [],
                "selected_action": "sequential",
                "outcome": "proceed_sequential",
                "fallback_plan": "Rerun overlap analysis before resuming parallel execution.",
                "validators_to_rerun": ["overlap_report", "verification"],
                "staleness_inputs": {
                    "execution_mode": "parallel",
                    "selected_task_ids": ["demo-0001"],
                    "selected_contract_paths": [str(Path("contract.yaml"))],
                    "overlap_report_sha256": "abc123",
                    "base_branch_head_commit": "deadbeef",
                    "release_inputs_sha256": "f00d",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="escapes artifact bundle"):
        load_supervisor_decision_artifact(artifact_path)


def test_execution_strategy_load_fails_for_missing_evidence_path(tmp_path: Path) -> None:
    decision = _execution_strategy_decision(evidence_paths=[Path("missing-strategy-evidence.log")])
    artifact_path = write_supervisor_decision_artifact(
        release_bundle_path=tmp_path,
        decision=decision,
    )

    with pytest.raises(ValueError, match="missing evidence path"):
        load_supervisor_decision_artifact(artifact_path)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "admission_failure_inputs": [
                {
                    "release_id": "planner-admission-repair",
                    "task_id": "planner-admission-repair-0002",
                    "validation_errors": ["too many files"],
                    "policy_constraints": ["keep bounded scope"],
                    "validators_to_rerun": ["contract_plan", "verification"],
                }
            ],
            "selected_action": "split_task",
            "outcome": "split_and_retry",
            "rationale": "Split to stay within bounded scope.",
            "fallback_plan": "Stop if split still fails admission.",
            "validators_to_rerun": ["contract_plan", "verification"],
            "evidence_paths": [Path("runs/release/supervisor.log")],
            "split_task_ids": ["planner-admission-repair-0002a"],
        },
        {
            "admission_failure_inputs": [
                {
                    "release_id": "planner-admission-repair",
                    "task_id": "planner-admission-repair-0002",
                    "validation_errors": ["scope too broad"],
                    "policy_constraints": ["keep bounded scope"],
                    "validators_to_rerun": ["contract_plan", "verification"],
                }
            ],
            "selected_action": "narrow_scope",
            "outcome": "narrow_and_retry",
            "rationale": "Narrow to a subset of allowed files.",
            "fallback_plan": "Replan if narrowing blocks required implementation.",
            "validators_to_rerun": ["contract_plan", "verification"],
            "evidence_paths": [Path("runs/release/supervisor.log")],
            "narrowed_allowed_files": ["src/agentic_devloop/runtime_supervisor.py"],
        },
        {
            "admission_failure_inputs": [
                {
                    "release_id": "planner-admission-repair",
                    "task_id": "planner-admission-repair-0002",
                    "validation_errors": ["file budget exceeded"],
                    "policy_constraints": ["mechanical broad changes can be accepted with guardrails"],
                    "validators_to_rerun": ["contract_plan", "verification"],
                }
            ],
            "selected_action": "accept_broad_but_mechanical",
            "outcome": "accept_with_mechanical_guards",
            "rationale": "Broad edits are mechanical and low semantic risk.",
            "fallback_plan": "Split if verification detects semantic drift.",
            "validators_to_rerun": ["contract_plan", "verification"],
            "evidence_paths": [Path("runs/release/supervisor.log")],
            "accepted_scope_notes": ["Mechanical rename only"],
        },
        {
            "admission_failure_inputs": [
                {
                    "release_id": "planner-admission-repair",
                    "task_id": "planner-admission-repair-0002",
                    "validation_errors": ["dependency cycle"],
                    "policy_constraints": ["depends_on must remain acyclic"],
                    "validators_to_rerun": ["contract_plan", "verification"],
                }
            ],
            "selected_action": "replan",
            "outcome": "replan_and_retry",
            "rationale": "Requires planner re-decomposition.",
            "fallback_plan": "Stop if replanning repeats invalid dependencies.",
            "validators_to_rerun": ["contract_plan", "verification"],
            "evidence_paths": [Path("runs/release/supervisor.log")],
            "replan_reason": "Graph constraints require fresh plan synthesis.",
        },
        {
            "admission_failure_inputs": [
                {
                    "release_id": "planner-admission-repair",
                    "task_id": "planner-admission-repair-0002",
                    "validation_errors": ["unsafe policy expansion"],
                    "policy_constraints": ["forbidden changes remain hard-gated"],
                    "validators_to_rerun": ["contract_plan", "verification"],
                }
            ],
            "selected_action": "stop",
            "outcome": "stop_and_escalate",
            "rationale": "Cannot safely repair within policy boundaries.",
            "fallback_plan": "Escalate with evidence bundle.",
            "validators_to_rerun": ["contract_plan", "verification"],
            "evidence_paths": [Path("runs/release/supervisor.log")],
            "stop_reason": "Admission repair would violate hard policy.",
        },
    ],
)
def test_planner_admission_repair_actions_serialize_with_required_fields(payload: dict[str, object]) -> None:
    typed_payload = PlannerAdmissionRepairActionPayload.model_validate(payload)
    serialized = typed_payload.model_dump(mode="json")

    assert serialized["rationale"]
    assert serialized["fallback_plan"]
    assert serialized["validators_to_rerun"]
    assert serialized["evidence_paths"]
