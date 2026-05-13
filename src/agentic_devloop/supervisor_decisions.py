from __future__ import annotations

import json
import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator

from agentic_devloop.models import ContractNormalizationRefusalReason, StrictModel


SCHEMA_VERSION_V1 = "1.0"


class DecisionRiskLevel(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class SupervisorDecisionType(StrEnum):
    RELEASE_SCHEDULING = "release_scheduling"
    REPAIR_LOOP_CONTINUATION = "repair_loop_continuation"
    REVIEW_FINDING_ADJUDICATION = "review_finding_adjudication"
    SOFT_BUDGET_ACCEPTANCE = "soft_budget_acceptance"
    CONTRACT_NORMALIZATION = "contract_normalization"
    ENVIRONMENT_REPAIR = "environment_repair"


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


class ReleaseSchedulingDecision(SupervisorDecisionBase):
    decision_type: Literal[SupervisorDecisionType.RELEASE_SCHEDULING] = SupervisorDecisionType.RELEASE_SCHEDULING
    risk_level: DecisionRiskLevel
    overlap_findings: list[str] = Field(default_factory=list)
    outcome: SchedulingOutcome

    @field_validator("overlap_findings")
    @classmethod
    def overlap_findings_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("overlap findings must not be empty")
        return values


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
        | RepairLoopContinuationDecision
        | ReviewFindingAdjudicationDecision
        | SoftBudgetAcceptanceDecision
        | ContractNormalizationDecision
        | EnvironmentRepairDecision
    ),
    Field(discriminator="decision_type"),
]

_SUPERVISOR_DECISION_ADAPTER = TypeAdapter(SupervisorDecisionRecord)


def parse_supervisor_decision(payload: object) -> SupervisorDecisionRecord:
    return _SUPERVISOR_DECISION_ADAPTER.validate_python(payload)


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
    decision = parse_supervisor_decision(payload)
    _validate_evidence_paths(decision=decision, artifact_path=path)
    return decision


def _validate_evidence_paths(*, decision: SupervisorDecisionRecord, artifact_path: Path) -> None:
    artifact_dir = artifact_path.parent
    for evidence_path in decision.evidence_paths:
        candidate = evidence_path if evidence_path.is_absolute() else artifact_dir / evidence_path
        if not candidate.exists():
            raise ValueError(
                f"missing evidence path in supervisor decision artifact {artifact_path}: {evidence_path}"
            )
