from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


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
