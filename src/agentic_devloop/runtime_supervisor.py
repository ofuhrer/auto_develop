from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agentic_devloop.contracts import normalize_planner_contract_plan_payload
from agentic_devloop.models import (
    ContractPlan,
    ModelOutputNormalizationActionPayload,
    TaskContract,
)
from agentic_devloop.supervisor_decisions import (
    DecisionRiskLevel,
    ModelOutputNormalizationDecision,
    build_model_output_normalization_decision,
)


class ReleaseEventKind(StrEnum):
    RELEASE_STARTED = "release_started"
    TASK_FAILED = "task_failed"
    VERIFICATION_FAILED = "verification_failed"
    RELEASE_BLOCKED = "release_blocked"
    RELEASE_COMPLETED = "release_completed"


@dataclass(frozen=True)
class ReleaseEvent:
    kind: ReleaseEventKind
    message: str
    event_path: Path


@dataclass(frozen=True)
class ReleaseSummaryReference:
    release_id: str
    summary_path: Path


@dataclass(frozen=True)
class EvidenceBundlePaths:
    bundle_path: Path
    changed_files_path: Path
    verification_log_path: Path


@dataclass(frozen=True)
class RawLogPaths:
    supervisor_log_path: Path
    worker_stdout_path: Path
    worker_stderr_path: Path


@dataclass(frozen=True)
class BudgetLedgerPaths:
    repair_budget_ledger_path: Path
    retry_budget_ledger_path: Path


@dataclass(frozen=True)
class TuningReportPaths:
    model_tuning_report_path: Path
    verification_tuning_report_path: Path


@dataclass(frozen=True)
class BacklogStateReference:
    backlog_state_path: Path
    active_epic_id: str


class RepairDecisionClassification(StrEnum):
    VERIFICATION_ENVIRONMENT_DRIFT = "verification_environment_drift"
    PLANNER_CONTRACT_NON_NORMALIZED = "planner_contract_non_normalized"
    TASK_SCOPE_OVERBROAD = "task_scope_overbroad"
    RELEASE_RESUMABLE = "release_resumable"
    LONG_RUNNING_WORKER_ACTIVE = "long_running_worker_active"
    MODEL_CAPABILITY_MISMATCH = "model_capability_mismatch"
    REPO_STATE_STALE = "repo_state_stale"
    MISSING_CREDENTIALS = "missing_credentials"
    CONTRACT_BOUNDARY_VIOLATION = "contract_boundary_violation"
    UNSAFE_POLICY_EXPANSION = "unsafe_policy_expansion"
    EXHAUSTED_RETRY_BUDGET = "exhausted_retry_budget"


class RuntimeSupervisorDecisionKind(StrEnum):
    RETRY = "retry"
    STOP = "stop"


class RuntimeSupervisorStopReason(StrEnum):
    EXHAUSTED_RETRY_BUDGET = "exhausted_retry_budget"
    MISSING_CREDENTIALS = "missing_credentials"
    CONTRACT_BOUNDARY_VIOLATION = "contract_boundary_violation"
    UNSAFE_POLICY_EXPANSION = "unsafe_policy_expansion"


class RuntimeSupervisorApplierStopKind(StrEnum):
    BROADENS_ALLOWED_FILES = "broadens_allowed_files"
    EXCEEDS_TASK_BUDGET = "exceeds_task_budget"
    EXCEEDS_RETRY_BUDGET = "exceeds_retry_budget"
    BYPASSES_HARD_GATE = "bypasses_hard_gate"
    OUTSIDE_TEMP_EVIDENCE_PATH = "outside_temp_evidence_path"
    UNAVAILABLE_MODEL = "unavailable_model"


class RepairActionKind(StrEnum):
    ENVIRONMENT_REPAIR = "environment_repair"
    PLANNER_CONTRACT_NORMALIZATION = "planner_contract_normalization"
    TASK_SPLIT_OR_SCOPE_NARROWING = "task_split_or_scope_narrowing"
    RELEASE_RESUME = "release_resume"
    LONG_RUNNING_WORKER_INSPECTION = "long_running_worker_inspection"
    MODEL_ESCALATION = "model_escalation"
    REPO_STATE_UPDATE_PROPOSAL = "repo_state_update_proposal"


@dataclass(frozen=True)
class RuntimeSupervisorInput:
    classification: RepairDecisionClassification
    attempt: int
    max_retries: int
    release_event: ReleaseEvent
    release_summary: ReleaseSummaryReference
    evidence_bundle_paths: EvidenceBundlePaths
    raw_log_paths: RawLogPaths
    budget_ledger_paths: BudgetLedgerPaths
    tuning_report_paths: TuningReportPaths
    backlog_state_reference: BacklogStateReference

    @property
    def source_evidence_paths(self) -> tuple[Path, ...]:
        return (
            self.release_event.event_path,
            self.release_summary.summary_path,
            self.evidence_bundle_paths.bundle_path,
            self.evidence_bundle_paths.changed_files_path,
            self.evidence_bundle_paths.verification_log_path,
            self.raw_log_paths.supervisor_log_path,
            self.raw_log_paths.worker_stdout_path,
            self.raw_log_paths.worker_stderr_path,
            self.budget_ledger_paths.repair_budget_ledger_path,
            self.budget_ledger_paths.retry_budget_ledger_path,
            self.tuning_report_paths.model_tuning_report_path,
            self.tuning_report_paths.verification_tuning_report_path,
            self.backlog_state_reference.backlog_state_path,
        )


@dataclass(frozen=True)
class EnvironmentRepairAction:
    source_evidence_paths: tuple[Path, ...]
    action_kind: RepairActionKind = RepairActionKind.ENVIRONMENT_REPAIR


@dataclass(frozen=True)
class PlannerContractNormalizationAction:
    source_evidence_paths: tuple[Path, ...]
    action_kind: RepairActionKind = RepairActionKind.PLANNER_CONTRACT_NORMALIZATION


@dataclass(frozen=True)
class TaskSplitOrScopeNarrowingAction:
    source_evidence_paths: tuple[Path, ...]
    action_kind: RepairActionKind = RepairActionKind.TASK_SPLIT_OR_SCOPE_NARROWING


@dataclass(frozen=True)
class ReleaseResumeAction:
    source_evidence_paths: tuple[Path, ...]
    action_kind: RepairActionKind = RepairActionKind.RELEASE_RESUME


@dataclass(frozen=True)
class LongRunningWorkerInspectionAction:
    source_evidence_paths: tuple[Path, ...]
    action_kind: RepairActionKind = RepairActionKind.LONG_RUNNING_WORKER_INSPECTION


@dataclass(frozen=True)
class ModelEscalationAction:
    source_evidence_paths: tuple[Path, ...]
    action_kind: RepairActionKind = RepairActionKind.MODEL_ESCALATION


@dataclass(frozen=True)
class RepoStateUpdateProposalAction:
    source_evidence_paths: tuple[Path, ...]
    action_kind: RepairActionKind = RepairActionKind.REPO_STATE_UPDATE_PROPOSAL


@dataclass(frozen=True)
class RuntimeSupervisorApplierStopEvidence:
    action_kind: RepairActionKind
    kind: RuntimeSupervisorApplierStopKind
    reason: str


@dataclass(frozen=True)
class PlannerContractNormalizationProposal:
    normalized_plan: ContractPlan
    source_evidence_paths: tuple[Path, ...]
    action_kind: RepairActionKind = RepairActionKind.PLANNER_CONTRACT_NORMALIZATION


@dataclass(frozen=True)
class TaskSplitOrScopeNarrowingProposal:
    original_task_id: str
    narrowed_allowed_files: tuple[str, ...]
    split_task_ids: tuple[str, ...] = ()
    source_evidence_paths: tuple[Path, ...] = ()
    action_kind: RepairActionKind = RepairActionKind.TASK_SPLIT_OR_SCOPE_NARROWING


@dataclass(frozen=True)
class VerificationEnvironmentRepairEvidenceProposal:
    evidence_capture_path: Path
    capture_commands: tuple[str, ...]
    source_evidence_paths: tuple[Path, ...]
    action_kind: RepairActionKind = RepairActionKind.ENVIRONMENT_REPAIR


@dataclass(frozen=True)
class ReleaseResumeIntent:
    action_id: str
    retry_budget: int
    stop_reason_fallback: RuntimeSupervisorStopReason
    source_evidence_paths: tuple[Path, ...]
    action_kind: RepairActionKind = RepairActionKind.RELEASE_RESUME


@dataclass(frozen=True)
class LongRunningWorkerInspectionSummary:
    summary: str
    active: bool
    source_evidence_paths: tuple[Path, ...]
    action_kind: RepairActionKind = RepairActionKind.LONG_RUNNING_WORKER_INSPECTION


@dataclass(frozen=True)
class ModelEscalationRecommendation:
    current_model: str
    recommended_model: str
    reason: str
    source_evidence_paths: tuple[Path, ...]
    action_kind: RepairActionKind = RepairActionKind.MODEL_ESCALATION


@dataclass(frozen=True)
class RepoStateUpdateProposal:
    update_summary: str
    proposed_changes: tuple[str, ...]
    source_evidence_paths: tuple[Path, ...]
    action_kind: RepairActionKind = RepairActionKind.REPO_STATE_UPDATE_PROPOSAL


@dataclass(frozen=True)
class ModelOutputNormalizationDecisionProposal:
    decision: ModelOutputNormalizationDecision
    source_evidence_paths: tuple[Path, ...]
    action_kind: RepairActionKind = RepairActionKind.PLANNER_CONTRACT_NORMALIZATION


RepairProposal = (
    PlannerContractNormalizationProposal
    | TaskSplitOrScopeNarrowingProposal
    | VerificationEnvironmentRepairEvidenceProposal
    | ReleaseResumeIntent
    | LongRunningWorkerInspectionSummary
    | ModelEscalationRecommendation
    | RepoStateUpdateProposal
    | ModelOutputNormalizationDecisionProposal
)


@dataclass(frozen=True)
class RuntimeSupervisorApplierResult:
    action_kind: RepairActionKind
    applied: bool
    proposal: RepairProposal | None = None
    stop_evidence: RuntimeSupervisorApplierStopEvidence | None = None


RepairAction = (
    EnvironmentRepairAction
    | PlannerContractNormalizationAction
    | TaskSplitOrScopeNarrowingAction
    | ReleaseResumeAction
    | LongRunningWorkerInspectionAction
    | ModelEscalationAction
    | RepoStateUpdateProposalAction
)


@dataclass(frozen=True)
class RuntimeSupervisorDecision:
    classification: RepairDecisionClassification
    decision: RuntimeSupervisorDecisionKind
    retryable: bool
    reason: str
    attempt: int
    max_retries: int
    remaining_retries: int
    action: RepairAction | None = None
    stop_reason: RuntimeSupervisorStopReason | None = None


class RuntimeSupervisor:
    def decide(self, supervisor_input: RuntimeSupervisorInput) -> RuntimeSupervisorDecision:
        attempt = supervisor_input.attempt
        max_retries = supervisor_input.max_retries
        if attempt < 1:
            raise ValueError("attempt must be >= 1")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")

        classification = supervisor_input.classification
        remaining_retries = max(max_retries - attempt, 0)

        stop_reasons = {
            RepairDecisionClassification.MISSING_CREDENTIALS: RuntimeSupervisorStopReason.MISSING_CREDENTIALS,
            RepairDecisionClassification.CONTRACT_BOUNDARY_VIOLATION: RuntimeSupervisorStopReason.CONTRACT_BOUNDARY_VIOLATION,
            RepairDecisionClassification.UNSAFE_POLICY_EXPANSION: RuntimeSupervisorStopReason.UNSAFE_POLICY_EXPANSION,
            RepairDecisionClassification.EXHAUSTED_RETRY_BUDGET: RuntimeSupervisorStopReason.EXHAUSTED_RETRY_BUDGET,
        }
        if classification in stop_reasons:
            return RuntimeSupervisorDecision(
                classification=classification,
                decision=RuntimeSupervisorDecisionKind.STOP,
                retryable=False,
                reason=_stop_reason_text(stop_reasons[classification]),
                attempt=attempt,
                max_retries=max_retries,
                remaining_retries=remaining_retries,
                stop_reason=stop_reasons[classification],
            )

        retryable_actions = {
            RepairDecisionClassification.VERIFICATION_ENVIRONMENT_DRIFT: EnvironmentRepairAction,
            RepairDecisionClassification.PLANNER_CONTRACT_NON_NORMALIZED: PlannerContractNormalizationAction,
            RepairDecisionClassification.TASK_SCOPE_OVERBROAD: TaskSplitOrScopeNarrowingAction,
            RepairDecisionClassification.RELEASE_RESUMABLE: ReleaseResumeAction,
            RepairDecisionClassification.LONG_RUNNING_WORKER_ACTIVE: LongRunningWorkerInspectionAction,
            RepairDecisionClassification.MODEL_CAPABILITY_MISMATCH: ModelEscalationAction,
            RepairDecisionClassification.REPO_STATE_STALE: RepoStateUpdateProposalAction,
        }
        if classification in retryable_actions:
            if attempt < max_retries:
                action_cls = retryable_actions[classification]
                return RuntimeSupervisorDecision(
                    classification=classification,
                    decision=RuntimeSupervisorDecisionKind.RETRY,
                    retryable=True,
                    reason="Repair action selected within retry budget for a contract-contained failure.",
                    attempt=attempt,
                    max_retries=max_retries,
                    remaining_retries=remaining_retries,
                    action=action_cls(source_evidence_paths=supervisor_input.source_evidence_paths),
                )
            return RuntimeSupervisorDecision(
                classification=classification,
                decision=RuntimeSupervisorDecisionKind.STOP,
                retryable=False,
                reason="Retry budget exhausted for a contract-contained failure.",
                attempt=attempt,
                max_retries=max_retries,
                remaining_retries=0,
                stop_reason=RuntimeSupervisorStopReason.EXHAUSTED_RETRY_BUDGET,
            )

        raise ValueError(f"Unhandled repair decision classification: {classification}")

    def apply_planner_contract_normalization(
        self,
        *,
        source_evidence_paths: tuple[Path, ...],
        expected_release_id: str,
        candidate_plan: ContractPlan | dict[str, Any],
    ) -> RuntimeSupervisorApplierResult:
        try:
            normalized_candidate: ContractPlan | dict[str, Any] = candidate_plan
            if isinstance(candidate_plan, dict):
                normalized_candidate = normalize_planner_contract_plan_payload(
                    candidate_plan,
                    release_id=expected_release_id,
                )
            normalized = ContractPlan.model_validate(normalized_candidate)
            normalized = ContractPlan.model_validate(normalized.model_dump(mode="python"))
        except ValidationError:
            return RuntimeSupervisorApplierResult(
                action_kind=RepairActionKind.PLANNER_CONTRACT_NORMALIZATION,
                applied=False,
                stop_evidence=RuntimeSupervisorApplierStopEvidence(
                    action_kind=RepairActionKind.PLANNER_CONTRACT_NORMALIZATION,
                    kind=RuntimeSupervisorApplierStopKind.BYPASSES_HARD_GATE,
                    reason="Planner normalization failed ContractPlan/TaskContract validation.",
                ),
            )
        return RuntimeSupervisorApplierResult(
            action_kind=RepairActionKind.PLANNER_CONTRACT_NORMALIZATION,
            applied=True,
            proposal=PlannerContractNormalizationProposal(
                normalized_plan=normalized,
                source_evidence_paths=source_evidence_paths,
            ),
        )

    def apply_task_split_or_scope_narrowing(
        self,
        *,
        source_evidence_paths: tuple[Path, ...],
        original_contract: TaskContract,
        narrowed_allowed_files: Sequence[str],
        split_task_ids: Sequence[str] = (),
    ) -> RuntimeSupervisorApplierResult:
        original_scope = set(original_contract.allowed_files)
        narrowed_scope = set(narrowed_allowed_files)
        if not narrowed_scope or not narrowed_scope.issubset(original_scope):
            return RuntimeSupervisorApplierResult(
                action_kind=RepairActionKind.TASK_SPLIT_OR_SCOPE_NARROWING,
                applied=False,
                stop_evidence=RuntimeSupervisorApplierStopEvidence(
                    action_kind=RepairActionKind.TASK_SPLIT_OR_SCOPE_NARROWING,
                    kind=RuntimeSupervisorApplierStopKind.BROADENS_ALLOWED_FILES,
                    reason="Scope narrowing proposal is not a non-empty subset of original allowed_files.",
                ),
            )
        return RuntimeSupervisorApplierResult(
            action_kind=RepairActionKind.TASK_SPLIT_OR_SCOPE_NARROWING,
            applied=True,
            proposal=TaskSplitOrScopeNarrowingProposal(
                original_task_id=original_contract.task_id,
                narrowed_allowed_files=tuple(narrowed_allowed_files),
                split_task_ids=tuple(split_task_ids),
                source_evidence_paths=source_evidence_paths,
            ),
        )

    def apply_verification_environment_repair_evidence_capture(
        self,
        *,
        source_evidence_paths: tuple[Path, ...],
        temporary_evidence_dir: Path,
        evidence_capture_path: Path,
        capture_commands: Sequence[str],
    ) -> RuntimeSupervisorApplierResult:
        if not _is_relative_to(evidence_capture_path, temporary_evidence_dir):
            return RuntimeSupervisorApplierResult(
                action_kind=RepairActionKind.ENVIRONMENT_REPAIR,
                applied=False,
                stop_evidence=RuntimeSupervisorApplierStopEvidence(
                    action_kind=RepairActionKind.ENVIRONMENT_REPAIR,
                    kind=RuntimeSupervisorApplierStopKind.OUTSIDE_TEMP_EVIDENCE_PATH,
                    reason="Verification environment repair evidence path is outside temporary evidence directory.",
                ),
            )
        return RuntimeSupervisorApplierResult(
            action_kind=RepairActionKind.ENVIRONMENT_REPAIR,
            applied=True,
            proposal=VerificationEnvironmentRepairEvidenceProposal(
                evidence_capture_path=evidence_capture_path,
                capture_commands=tuple(capture_commands),
                source_evidence_paths=source_evidence_paths,
            ),
        )

    def apply_release_resume_intent(
        self,
        *,
        source_evidence_paths: tuple[Path, ...],
        action_id: str | None,
        retry_budget: int | None,
        stop_reason_fallback: RuntimeSupervisorStopReason | None,
    ) -> RuntimeSupervisorApplierResult:
        if not action_id or retry_budget is None or stop_reason_fallback is None:
            return RuntimeSupervisorApplierResult(
                action_kind=RepairActionKind.RELEASE_RESUME,
                applied=False,
                stop_evidence=RuntimeSupervisorApplierStopEvidence(
                    action_kind=RepairActionKind.RELEASE_RESUME,
                    kind=RuntimeSupervisorApplierStopKind.BYPASSES_HARD_GATE,
                    reason="Release resume requires action_id, retry_budget, and stop_reason_fallback.",
                ),
            )
        if retry_budget < 0:
            return RuntimeSupervisorApplierResult(
                action_kind=RepairActionKind.RELEASE_RESUME,
                applied=False,
                stop_evidence=RuntimeSupervisorApplierStopEvidence(
                    action_kind=RepairActionKind.RELEASE_RESUME,
                    kind=RuntimeSupervisorApplierStopKind.EXCEEDS_RETRY_BUDGET,
                    reason="Release resume retry_budget must be >= 0.",
                ),
            )
        return RuntimeSupervisorApplierResult(
            action_kind=RepairActionKind.RELEASE_RESUME,
            applied=True,
            proposal=ReleaseResumeIntent(
                action_id=action_id,
                retry_budget=retry_budget,
                stop_reason_fallback=stop_reason_fallback,
                source_evidence_paths=source_evidence_paths,
            ),
        )

    def apply_long_running_worker_inspection(
        self,
        *,
        source_evidence_paths: tuple[Path, ...],
        summary: str,
        active: bool,
    ) -> RuntimeSupervisorApplierResult:
        return RuntimeSupervisorApplierResult(
            action_kind=RepairActionKind.LONG_RUNNING_WORKER_INSPECTION,
            applied=True,
            proposal=LongRunningWorkerInspectionSummary(
                summary=summary,
                active=active,
                source_evidence_paths=source_evidence_paths,
            ),
        )

    def apply_model_escalation_recommendation(
        self,
        *,
        source_evidence_paths: tuple[Path, ...],
        current_model: str,
        recommended_model: str,
        reason: str,
        retry_budget_remaining: int,
        available_models: Sequence[str] | None = None,
    ) -> RuntimeSupervisorApplierResult:
        if available_models is not None and recommended_model not in set(available_models):
            return RuntimeSupervisorApplierResult(
                action_kind=RepairActionKind.MODEL_ESCALATION,
                applied=False,
                stop_evidence=RuntimeSupervisorApplierStopEvidence(
                    action_kind=RepairActionKind.MODEL_ESCALATION,
                    kind=RuntimeSupervisorApplierStopKind.UNAVAILABLE_MODEL,
                    reason="Model escalation recommended a model unavailable in configured routing.",
                ),
            )
        if retry_budget_remaining <= 0:
            return RuntimeSupervisorApplierResult(
                action_kind=RepairActionKind.MODEL_ESCALATION,
                applied=False,
                stop_evidence=RuntimeSupervisorApplierStopEvidence(
                    action_kind=RepairActionKind.MODEL_ESCALATION,
                    kind=RuntimeSupervisorApplierStopKind.EXCEEDS_RETRY_BUDGET,
                    reason="Model escalation requires remaining retry budget.",
                ),
            )
        return RuntimeSupervisorApplierResult(
            action_kind=RepairActionKind.MODEL_ESCALATION,
            applied=True,
            proposal=ModelEscalationRecommendation(
                current_model=current_model,
                recommended_model=recommended_model,
                reason=reason,
                source_evidence_paths=source_evidence_paths,
            ),
        )

    def apply_repo_state_update_proposal(
        self,
        *,
        source_evidence_paths: tuple[Path, ...],
        update_summary: str,
        proposed_changes: Sequence[str],
    ) -> RuntimeSupervisorApplierResult:
        return RuntimeSupervisorApplierResult(
            action_kind=RepairActionKind.REPO_STATE_UPDATE_PROPOSAL,
            applied=True,
            proposal=RepoStateUpdateProposal(
                update_summary=update_summary,
                proposed_changes=tuple(proposed_changes),
                source_evidence_paths=source_evidence_paths,
            ),
        )

    def apply_model_output_normalization_decision(
        self,
        *,
        source_evidence_paths: tuple[Path, ...],
        decision_id: str,
        release_id: str,
        decided_by: str,
        risk_level: DecisionRiskLevel,
        evidence_paths: list[Path],
        action_payload: ModelOutputNormalizationActionPayload,
        decided_at: datetime | None = None,
    ) -> RuntimeSupervisorApplierResult:
        decision = build_model_output_normalization_decision(
            decision_id=decision_id,
            release_id=release_id,
            decided_at=decided_at or datetime.now(),
            decided_by=decided_by,
            risk_level=risk_level,
            evidence_paths=evidence_paths,
            action_payload=action_payload,
        )
        return RuntimeSupervisorApplierResult(
            action_kind=RepairActionKind.PLANNER_CONTRACT_NORMALIZATION,
            applied=True,
            proposal=ModelOutputNormalizationDecisionProposal(
                decision=decision,
                source_evidence_paths=source_evidence_paths,
            ),
        )


def _stop_reason_text(reason: RuntimeSupervisorStopReason) -> str:
    if reason == RuntimeSupervisorStopReason.MISSING_CREDENTIALS:
        return "Required credentials are missing and must be provided externally."
    if reason == RuntimeSupervisorStopReason.CONTRACT_BOUNDARY_VIOLATION:
        return "Failure crossed the task contract boundary and requires escalation."
    if reason == RuntimeSupervisorStopReason.UNSAFE_POLICY_EXPANSION:
        return "Proposed repair would expand policy unsafely beyond configured boundaries."
    if reason == RuntimeSupervisorStopReason.EXHAUSTED_RETRY_BUDGET:
        return "Autonomous repair budget is exhausted."

    raise ValueError(f"Unhandled stop reason: {reason}")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
