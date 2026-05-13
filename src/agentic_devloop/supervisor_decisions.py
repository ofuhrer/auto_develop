from __future__ import annotations

import json
import re
import warnings
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator

from agentic_devloop.models import ContractNormalizationRefusalReason, StrictModel


SCHEMA_VERSION_V1 = "1.0"
LEGACY_VALIDATORS_UNSPECIFIED = "legacy_schema_v1_validators_unspecified"


class DecisionRiskLevel(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class SupervisorDecisionType(StrEnum):
    RELEASE_SCHEDULING = "release_scheduling"
    EXECUTION_STRATEGY = "execution_strategy"
    REPAIR_LOOP_CONTINUATION = "repair_loop_continuation"
    REVIEW_FINDING_ADJUDICATION = "review_finding_adjudication"
    SOFT_BUDGET_ACCEPTANCE = "soft_budget_acceptance"
    CONTRACT_NORMALIZATION = "contract_normalization"
    MODEL_OUTPUT_NORMALIZATION = "model_output_normalization"
    ENVIRONMENT_REPAIR = "environment_repair"
    FEATURE_REVIEW_FINDING_CLASSIFICATION = "feature_review_finding_classification"


_LEGACY_VALIDATORS_DECISION_TYPES = {
    SupervisorDecisionType.RELEASE_SCHEDULING.value,
    SupervisorDecisionType.EXECUTION_STRATEGY.value,
    SupervisorDecisionType.MODEL_OUTPUT_NORMALIZATION.value,
}


class SupervisorDecisionBase(StrictModel):
    schema_version: Literal[SCHEMA_VERSION_V1] = SCHEMA_VERSION_V1
    decision_id: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    decided_at: datetime
    decided_by: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    evidence_paths: list[Path] = Field(default_factory=list)


class SchedulingOutcome(StrEnum):
    PROCEED_PARALLEL = "proceed_parallel"
    PROCEED_SEQUENTIAL = "proceed_sequential"
    STACKED_BRANCHES = "stacked_branches"
    REPLAN = "replan"
    STOP = "stop"


class ReleaseSchedulingAction(StrEnum):
    PARALLEL = "parallel"
    SEQUENTIAL = "sequential"
    STACKED = "stacked"
    REPLAN = "replan"
    STOP = "stop"


class ReleaseSchedulingStalenessInputs(StrictModel):
    execution_mode: Literal["sequential", "parallel"]
    selected_task_ids: list[str] = Field(default_factory=list)
    selected_contract_paths: list[Path] = Field(default_factory=list)
    overlap_report_sha256: str = Field(min_length=1)
    base_branch_head_commit: str = Field(min_length=1)
    release_inputs_sha256: str = Field(min_length=1)

    @field_validator("selected_task_ids")
    @classmethod
    def selected_task_ids_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("selected task ids must not be empty")
        return values

    @field_validator("selected_contract_paths")
    @classmethod
    def selected_contract_paths_must_not_be_empty(cls, values: list[Path]) -> list[Path]:
        if not values:
            raise ValueError("selected contract paths must not be empty")
        return values

    @model_validator(mode="after")
    def staleness_inputs_must_be_consistent(self) -> "ReleaseSchedulingStalenessInputs":
        if len(self.selected_task_ids) != len(self.selected_contract_paths):
            raise ValueError("selected task ids and contract paths must have the same length")
        if len(self.selected_task_ids) != len(set(self.selected_task_ids)):
            raise ValueError("selected task ids must be unique")
        if len(self.selected_contract_paths) != len(set(self.selected_contract_paths)):
            raise ValueError("selected contract paths must be unique")
        return self


class ReleaseSchedulingDecision(SupervisorDecisionBase):
    decision_type: Literal[SupervisorDecisionType.RELEASE_SCHEDULING] = SupervisorDecisionType.RELEASE_SCHEDULING
    risk_level: DecisionRiskLevel
    overlap_findings: list[str] = Field(default_factory=list)
    selected_action: ReleaseSchedulingAction
    outcome: SchedulingOutcome
    fallback_plan: str = Field(min_length=1)
    validators_to_rerun: list[str]
    staleness_inputs: ReleaseSchedulingStalenessInputs

    @field_validator("overlap_findings")
    @classmethod
    def overlap_findings_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("overlap findings must not be empty")
        return values

    @field_validator("validators_to_rerun")
    @classmethod
    def validators_to_rerun_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if not values or any(not value.strip() for value in values):
            raise ValueError("validators to rerun must not be empty")
        return values

    @model_validator(mode="after")
    def selected_action_must_match_outcome(self) -> "ReleaseSchedulingDecision":
        outcome_by_action = {
            ReleaseSchedulingAction.PARALLEL: SchedulingOutcome.PROCEED_PARALLEL,
            ReleaseSchedulingAction.SEQUENTIAL: SchedulingOutcome.PROCEED_SEQUENTIAL,
            ReleaseSchedulingAction.STACKED: SchedulingOutcome.STACKED_BRANCHES,
            ReleaseSchedulingAction.REPLAN: SchedulingOutcome.REPLAN,
            ReleaseSchedulingAction.STOP: SchedulingOutcome.STOP,
        }
        expected_outcome = outcome_by_action[self.selected_action]
        if self.outcome != expected_outcome:
            raise ValueError("selected_action must match outcome")
        return self


class ExecutionStrategyOutcome(StrEnum):
    PROCEED_ONE_SHOT = "proceed_one_shot"
    PROCEED_SEQUENTIAL = "proceed_sequential"
    PROCEED_PARALLEL = "proceed_parallel"
    PROCEED_STACKED = "proceed_stacked"
    PROCEED_PATCH_HANDOFF = "proceed_patch_handoff"
    REPLAN = "replan"


class ExecutionStrategyAction(StrEnum):
    ONE_SHOT = "one_shot"
    SEQUENTIAL_CONTRACTS = "sequential_contracts"
    PARALLEL_CONTRACTS = "parallel_contracts"
    STACKED_BRANCHES = "stacked_branches"
    PATCH_HANDOFF = "patch_handoff"
    REPLAN = "replan"


class ExecutionStrategyDecision(SupervisorDecisionBase):
    decision_type: Literal[SupervisorDecisionType.EXECUTION_STRATEGY] = SupervisorDecisionType.EXECUTION_STRATEGY
    risk_level: DecisionRiskLevel
    selected_action: ExecutionStrategyAction
    outcome: ExecutionStrategyOutcome
    fallback_plan: str = Field(min_length=1)
    validators_to_rerun: list[str]

    @field_validator("validators_to_rerun")
    @classmethod
    def validators_to_rerun_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if not values or any(not value.strip() for value in values):
            raise ValueError("validators to rerun must not be empty")
        return values

    @model_validator(mode="after")
    def selected_action_must_match_outcome(self) -> "ExecutionStrategyDecision":
        outcome_by_action = {
            ExecutionStrategyAction.ONE_SHOT: ExecutionStrategyOutcome.PROCEED_ONE_SHOT,
            ExecutionStrategyAction.SEQUENTIAL_CONTRACTS: ExecutionStrategyOutcome.PROCEED_SEQUENTIAL,
            ExecutionStrategyAction.PARALLEL_CONTRACTS: ExecutionStrategyOutcome.PROCEED_PARALLEL,
            ExecutionStrategyAction.STACKED_BRANCHES: ExecutionStrategyOutcome.PROCEED_STACKED,
            ExecutionStrategyAction.PATCH_HANDOFF: ExecutionStrategyOutcome.PROCEED_PATCH_HANDOFF,
            ExecutionStrategyAction.REPLAN: ExecutionStrategyOutcome.REPLAN,
        }
        expected_outcome = outcome_by_action[self.selected_action]
        if self.outcome != expected_outcome:
            raise ValueError("selected_action must match outcome")
        return self


class RepairLoopOutcome(StrEnum):
    RETRY_REPAIR = "retry_repair"
    RESUME_RELEASE = "resume_release"
    ESCALATE = "escalate"
    STOP = "stop"


class RepairLoopContinuationDecision(SupervisorDecisionBase):
    decision_type: Literal[SupervisorDecisionType.REPAIR_LOOP_CONTINUATION] = (
        SupervisorDecisionType.REPAIR_LOOP_CONTINUATION
    )
    risk_level: DecisionRiskLevel
    attempt: int = Field(ge=1)
    max_attempts: int = Field(ge=1)
    outcome: RepairLoopOutcome

    @model_validator(mode="after")
    def attempt_must_not_exceed_max_attempts(self) -> "RepairLoopContinuationDecision":
        if self.attempt > self.max_attempts:
            raise ValueError("attempt must be less than or equal to max_attempts")
        return self


class FindingSeverity(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class FindingAdjudicationOutcome(StrEnum):
    REQUIRED_REPAIR = "required_repair"
    ACCEPTED_RISK = "accepted_risk"
    FALSE_POSITIVE = "false_positive"
    OUT_OF_SCOPE_FOLLOW_UP = "out_of_scope_follow_up"


class ReviewFindingAdjudicationDecision(SupervisorDecisionBase):
    decision_type: Literal[SupervisorDecisionType.REVIEW_FINDING_ADJUDICATION] = (
        SupervisorDecisionType.REVIEW_FINDING_ADJUDICATION
    )
    finding_id: str = Field(min_length=1)
    severity: FindingSeverity
    outcome: FindingAdjudicationOutcome
    repair_task_ids: list[str] = Field(default_factory=list)

    @field_validator("repair_task_ids")
    @classmethod
    def repair_task_ids_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("repair task IDs must not be empty")
        return values


class FeatureReviewFindingClassification(StrEnum):
    BLOCKER = "blocker"
    SOFT_FINDING = "soft_finding"
    DUPLICATE = "duplicate"
    FALSE_POSITIVE = "false_positive"
    SCOPE_EXPANSION = "scope_expansion"
    BACKLOG_FOLLOW_UP = "backlog_follow_up"


class FeatureReviewFindingAction(StrEnum):
    REPAIR = "repair"
    ACCEPT = "accept"
    DEFER = "defer"


class FeatureReviewFindingOutcome(StrEnum):
    CONTINUE = "continue"
    STOP_FINDING = "stop"
    STOP = STOP_FINDING


class FeatureReviewFindingClassificationDecision(SupervisorDecisionBase):
    decision_type: Literal[SupervisorDecisionType.FEATURE_REVIEW_FINDING_CLASSIFICATION] = (
        SupervisorDecisionType.FEATURE_REVIEW_FINDING_CLASSIFICATION
    )
    finding_id: str = Field(min_length=1)
    classification: FeatureReviewFindingClassification
    selected_action: FeatureReviewFindingAction
    outcome: FeatureReviewFindingOutcome
    fallback_plan: str = Field(min_length=1)
    validators_to_rerun: list[str]

    @field_validator("validators_to_rerun")
    @classmethod
    def validators_to_rerun_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if not values or any(not value.strip() for value in values):
            raise ValueError("validators to rerun must not be empty")
        return values

    @model_validator(mode="after")
    def classification_rules_must_be_satisfied(self) -> "FeatureReviewFindingClassificationDecision":
        if self.selected_action in {FeatureReviewFindingAction.REPAIR, FeatureReviewFindingAction.ACCEPT}:
            if self.outcome != FeatureReviewFindingOutcome.CONTINUE:
                raise ValueError("repair or accept requires continue outcome")
        if self.selected_action == FeatureReviewFindingAction.DEFER and self.outcome != FeatureReviewFindingOutcome.STOP_FINDING:
            raise ValueError("defer requires stop outcome")
        if (
            self.classification == FeatureReviewFindingClassification.BLOCKER
            and self.selected_action == FeatureReviewFindingAction.ACCEPT
        ):
            raise ValueError("blocker classification must not use accept action")
        if (
            self.classification == FeatureReviewFindingClassification.DUPLICATE
            and self.selected_action == FeatureReviewFindingAction.ACCEPT
        ):
            raise ValueError("duplicate classification must not use accept action")
        if (
            self.classification != FeatureReviewFindingClassification.BLOCKER
            and self.selected_action == FeatureReviewFindingAction.ACCEPT
            and not self.evidence_paths
        ):
            raise ValueError("non-blocking accepted classification requires evidence_paths")
        return self


class BudgetAcceptanceOutcome(StrEnum):
    ACCEPT_OVERAGE = "accept_overage"
    SPLIT_TASK = "split_task"
    NARROW_SCOPE_AND_RETRY = "narrow_scope_and_retry"
    STOP = "stop"


class SoftBudgetAcceptanceDecision(SupervisorDecisionBase):
    decision_type: Literal[SupervisorDecisionType.SOFT_BUDGET_ACCEPTANCE] = (
        SupervisorDecisionType.SOFT_BUDGET_ACCEPTANCE
    )
    budget_name: str = Field(min_length=1)
    configured_limit: float = Field(gt=0)
    actual: float = Field(ge=0)
    outcome: BudgetAcceptanceOutcome

    @model_validator(mode="after")
    def actual_must_reach_limit(self) -> "SoftBudgetAcceptanceDecision":
        if self.actual < self.configured_limit:
            raise ValueError("actual must be greater than or equal to configured_limit")
        return self


class ContractNormalizationOutcome(StrEnum):
    NORMALIZE_AND_RETRY = "normalize_and_retry"
    REFUSE_AND_STOP = "refuse_and_stop"


class ContractNormalizationDecision(SupervisorDecisionBase):
    decision_type: Literal[SupervisorDecisionType.CONTRACT_NORMALIZATION] = SupervisorDecisionType.CONTRACT_NORMALIZATION
    outcome: ContractNormalizationOutcome
    changed_fields: list[str] = Field(default_factory=list)
    refusal_reasons: list[ContractNormalizationRefusalReason] = Field(default_factory=list)

    @field_validator("changed_fields")
    @classmethod
    def changed_fields_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("changed fields must not be empty")
        return values

    @model_validator(mode="after")
    def outcome_must_match_details(self) -> "ContractNormalizationDecision":
        if self.outcome == ContractNormalizationOutcome.NORMALIZE_AND_RETRY and not self.changed_fields:
            raise ValueError("normalize_and_retry requires changed_fields")
        if self.outcome == ContractNormalizationOutcome.REFUSE_AND_STOP and not self.refusal_reasons:
            raise ValueError("refuse_and_stop requires refusal_reasons")
        return self


class ModelOutputNormalizationAction(StrEnum):
    APPLY_NORMALIZATION = "apply_normalization"
    REFUSE = "refuse"


class ModelOutputNormalizationOutcome(StrEnum):
    NORMALIZED_AND_RETRY = "normalized_and_retry"
    REFUSED_AND_STOP = "refused_and_stop"


class ModelOutputValidationError(StrictModel):
    field: str = Field(min_length=1)
    message: str = Field(min_length=1)
    error_type: str = Field(min_length=1)


class ModelOutputNormalizationDecision(SupervisorDecisionBase):
    decision_type: Literal[SupervisorDecisionType.MODEL_OUTPUT_NORMALIZATION] = (
        SupervisorDecisionType.MODEL_OUTPUT_NORMALIZATION
    )
    risk_level: DecisionRiskLevel
    raw_artifact_paths: list[Path] = Field(default_factory=list)
    validation_errors: list[ModelOutputValidationError] = Field(default_factory=list)
    selected_action: ModelOutputNormalizationAction
    outcome: ModelOutputNormalizationOutcome
    fallback_plan: str = Field(min_length=1)
    validators_to_rerun: list[str]
    normalized_artifact_path: Path | None = None
    refusal_reason: str | None = None

    @field_validator("raw_artifact_paths")
    @classmethod
    def raw_artifact_paths_must_not_be_empty(cls, values: list[Path]) -> list[Path]:
        if not values:
            raise ValueError("raw artifact paths must not be empty")
        return values

    @field_validator("validators_to_rerun")
    @classmethod
    def validators_to_rerun_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("validators to rerun must not include empty values")
        return values

    @model_validator(mode="after")
    def selected_action_must_match_outcome(self) -> "ModelOutputNormalizationDecision":
        outcome_by_action = {
            ModelOutputNormalizationAction.APPLY_NORMALIZATION: ModelOutputNormalizationOutcome.NORMALIZED_AND_RETRY,
            ModelOutputNormalizationAction.REFUSE: ModelOutputNormalizationOutcome.REFUSED_AND_STOP,
        }
        expected_outcome = outcome_by_action[self.selected_action]
        if self.outcome != expected_outcome:
            raise ValueError("selected_action must match outcome")
        if self.outcome == ModelOutputNormalizationOutcome.NORMALIZED_AND_RETRY:
            if not self.validation_errors:
                raise ValueError("normalized_and_retry requires validation_errors")
            if not effective_validators_to_rerun(self.validators_to_rerun):
                raise ValueError(
                    "normalized_and_retry requires explicit validators_to_rerun; "
                    "legacy_schema_v1_validators_unspecified is not runnable"
                )
            if self.normalized_artifact_path is None:
                raise ValueError("normalized_and_retry requires normalized_artifact_path")
            if self.refusal_reason is not None:
                raise ValueError("normalized_and_retry must not include refusal_reason")
        if self.outcome == ModelOutputNormalizationOutcome.REFUSED_AND_STOP:
            if self.refusal_reason is None or not self.refusal_reason.strip():
                raise ValueError("refused_and_stop requires refusal_reason")
            if self.normalized_artifact_path is not None:
                raise ValueError("refused_and_stop must not include normalized_artifact_path")
        return self


class EnvironmentRepairOutcome(StrEnum):
    APPLY_AND_RETRY = "apply_and_retry"
    CAPTURE_ONLY = "capture_only"
    ESCALATE = "escalate"
    STOP = "stop"


class EnvironmentRepairDecision(SupervisorDecisionBase):
    decision_type: Literal[SupervisorDecisionType.ENVIRONMENT_REPAIR] = SupervisorDecisionType.ENVIRONMENT_REPAIR
    outcome: EnvironmentRepairOutcome
    capture_commands: list[str] = Field(default_factory=list)

    @field_validator("capture_commands")
    @classmethod
    def capture_commands_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("capture commands must not be empty")
        return values


SupervisorDecisionRecord = Annotated[
    (
        ReleaseSchedulingDecision
        | ExecutionStrategyDecision
        | RepairLoopContinuationDecision
        | ReviewFindingAdjudicationDecision
        | SoftBudgetAcceptanceDecision
        | ContractNormalizationDecision
        | ModelOutputNormalizationDecision
        | EnvironmentRepairDecision
        | FeatureReviewFindingClassificationDecision
    ),
    Field(discriminator="decision_type"),
]

_SUPERVISOR_DECISION_ADAPTER = TypeAdapter(SupervisorDecisionRecord)


def parse_supervisor_decision(payload: object) -> SupervisorDecisionRecord:
    normalized_payload, _ = _normalize_legacy_supervisor_decision_payload(payload)
    return _SUPERVISOR_DECISION_ADAPTER.validate_python(normalized_payload)


def effective_validators_to_rerun(validators_to_rerun: list[str]) -> list[str]:
    """Return concrete validators; the legacy sentinel means unspecified."""
    if validators_to_rerun == [LEGACY_VALIDATORS_UNSPECIFIED]:
        return []
    return validators_to_rerun


def _normalize_legacy_supervisor_decision_payload(payload: object) -> tuple[object, bool]:
    if not isinstance(payload, dict):
        return payload, False
    decision_type = payload.get("decision_type")
    if (
        payload.get("schema_version") != SCHEMA_VERSION_V1
        or decision_type not in _LEGACY_VALIDATORS_DECISION_TYPES
        or "validators_to_rerun" in payload
    ):
        return payload, False

    normalized_payload = dict(payload)
    normalized_payload["validators_to_rerun"] = [LEGACY_VALIDATORS_UNSPECIFIED]
    return normalized_payload, True


def supervisor_decision_artifact_path(
    *,
    release_bundle_path: Path,
    decision_type: SupervisorDecisionType,
    decision_id: str,
) -> Path:
    normalized_decision_id = _safe_decision_filename_token(decision_id)
    filename = f"{decision_type.value}__{normalized_decision_id}.json"
    return release_bundle_path / "supervisor_decisions" / filename


def _safe_decision_filename_token(decision_id: str) -> str:
    normalized_decision_id = decision_id.strip()
    if (
        not normalized_decision_id
        or "/" in normalized_decision_id
        or "\\" in normalized_decision_id
        or ".." in normalized_decision_id
    ):
        raise ValueError("decision_id must be a non-empty filename token without path separators or '..'")
    safe_token = re.sub(r"[^A-Za-z0-9._-]+", "_", normalized_decision_id).strip("._")
    if not safe_token:
        raise ValueError("decision_id must not be empty")
    return safe_token


def write_supervisor_decision_artifact(
    *,
    release_bundle_path: Path,
    decision: SupervisorDecisionRecord,
) -> Path:
    artifact_path = supervisor_decision_artifact_path(
        release_bundle_path=release_bundle_path,
        decision_type=decision.decision_type,
        decision_id=decision.decision_id,
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(decision.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return artifact_path


def load_supervisor_decision_artifact(path: Path) -> SupervisorDecisionRecord:
    if not path.exists():
        raise FileNotFoundError(f"supervisor decision artifact does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    normalized_payload, migrated = _normalize_legacy_supervisor_decision_payload(payload)
    if migrated:
        warnings.warn(
            f"loaded legacy supervisor decision artifact without validators_to_rerun: {path}",
            UserWarning,
            stacklevel=2,
        )
    decision = _SUPERVISOR_DECISION_ADAPTER.validate_python(normalized_payload)
    _validate_evidence_paths(decision=decision, artifact_path=path)
    return decision


def _validate_evidence_paths(*, decision: SupervisorDecisionRecord, artifact_path: Path) -> None:
    artifact_dir = artifact_path.parent.resolve()
    artifact_bundle_dir = artifact_dir.parent
    for evidence_path in decision.evidence_paths:
        candidate = evidence_path if evidence_path.is_absolute() else artifact_bundle_dir / evidence_path
        if not evidence_path.is_absolute():
            candidate_resolved = candidate.resolve()
            if not candidate_resolved.is_relative_to(artifact_bundle_dir):
                raise ValueError(
                    f"supervisor decision evidence path escapes artifact bundle: {evidence_path}"
                )
        if not candidate.exists():
            raise ValueError(
                f"missing evidence path in supervisor decision artifact {artifact_path}: {evidence_path}"
            )
