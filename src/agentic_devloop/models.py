from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecutorConfig(StrictModel):
    type: str = Field(min_length=1)
    model: str = Field(min_length=1)
    max_walltime_minutes: int = Field(gt=0)
    fallback_models: list[str] = Field(default_factory=list)

    @field_validator("fallback_models")
    @classmethod
    def fallback_models_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("fallback models must not be empty")
        return values


class ModelAvailability(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class ModelCatalogEntry(StrictModel):
    model: str = Field(min_length=1)
    capabilities: list[str] = Field(default_factory=list)
    budget_class: str = Field(min_length=1)
    availability: ModelAvailability = ModelAvailability.UNKNOWN

    @field_validator("capabilities")
    @classmethod
    def capabilities_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("model capabilities must not be empty")
        return values


class VerificationProfile(StrictModel):
    commands: list[str] = Field(min_length=1)

    @field_validator("commands")
    @classmethod
    def commands_must_not_be_empty(cls, commands: list[str]) -> list[str]:
        if any(not command.strip() for command in commands):
            raise ValueError("verification commands must not be empty")
        return commands


class Budget(StrictModel):
    max_executor_attempts_per_task: int = Field(gt=0)
    max_strong_model_calls_per_release: int = Field(ge=0)
    max_changed_files_per_task: int = Field(gt=0)
    max_diff_lines_per_task: int = Field(gt=0)
    max_context_chars_per_task: int = Field(default=30_000, gt=0)


class TaskType(StrEnum):
    CODE_ONLY = "code_only"
    DOCUMENTATION = "documentation"
    BENCHMARK = "benchmark"
    SCIENTIFIC_VALIDATION = "scientific_validation"
    RELEASE_PREPARATION = "release_preparation"


class ModelRouting(StrictModel):
    default_role: str = Field(default="worker", min_length=1)
    task_type_roles: dict[TaskType, str] = Field(default_factory=dict)
    budget_class_roles: dict[str, str] = Field(default_factory=dict)
    escalation_role: str | None = None


class ProjectConfig(StrictModel):
    project_id: str = Field(min_length=1)
    repo_path: Path
    default_base_branch: str = Field(min_length=1)
    worktree_root: Path
    executor: ExecutorConfig
    model_catalog: dict[str, ModelCatalogEntry] = Field(default_factory=dict)
    model_roles: dict[str, ExecutorConfig] = Field(default_factory=dict)
    model_routing: ModelRouting = Field(default_factory=ModelRouting)
    verification_profiles: dict[str, VerificationProfile] = Field(min_length=1)
    unsafe_overlap_paths: list[str] = Field(default_factory=list)
    budget: Budget
    repo_state_path: Path | None = None

    @field_validator("unsafe_overlap_paths")
    @classmethod
    def unsafe_overlap_paths_must_not_be_empty_or_repo_wide(cls, values: list[str]) -> list[str]:
        broad = {"*", "**", "**/*", "./**", "./**/*"}
        for value in values:
            normalized = value.strip()
            if not normalized:
                raise ValueError("unsafe overlap paths must not be empty")
            if normalized in broad:
                raise ValueError("unsafe overlap paths must not include repo-wide globs")
        return values

    @model_validator(mode="after")
    def model_routing_roles_must_exist(self) -> "ProjectConfig":
        available_roles = set(self.model_roles)
        referenced_roles = {self.model_routing.default_role}
        referenced_roles.update(self.model_routing.task_type_roles.values())
        referenced_roles.update(self.model_routing.budget_class_roles.values())
        if self.model_routing.escalation_role:
            referenced_roles.add(self.model_routing.escalation_role)

        missing_roles = sorted(referenced_roles - available_roles)
        if missing_roles and self.model_roles:
            raise ValueError(f"model routing references undefined roles: {', '.join(missing_roles)}")
        return self


class ReleaseObjective(StrictModel):
    release_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    non_goals: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(min_length=1)


class BacklogEpic(StrictModel):
    epic_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    priority: int = Field(ge=1)
    source_refs: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(min_length=1)
    suggested_release_id: str = Field(min_length=1)

    @field_validator("source_refs", "acceptance_criteria")
    @classmethod
    def backlog_items_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("backlog list items must not be empty")
        return values


class BacklogPlan(StrictModel):
    project_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    roadmap_path: Path
    planner: str = Field(default="deterministic")
    epics: list[BacklogEpic] = Field(default_factory=list)
    selected_epic_id: str | None = None
    objective_path: Path | None = None
    warnings: list[str] = Field(default_factory=list)
    roadmap_updates: list[str] = Field(default_factory=list)
    repo_state_updates: list[str] = Field(default_factory=list)
    planner_prompt_path: Path | None = None
    planner_stdout_path: Path | None = None
    planner_stderr_path: Path | None = None
    planner_metadata_path: Path | None = None
    state_review_snapshot_path: Path | None = None


class BacklogEvidenceManifest(StrictModel):
    backlog_plan_path: Path | None = None
    generated_objective_path: Path | None = None
    contract_plan_path: Path | None = None
    release_summary_path: Path | None = None
    release_metrics_path: Path | None = None
    release_budget_path: Path | None = None
    release_tuning_path: Path | None = None
    state_review_snapshot_path: Path | None = None


class StateReviewSnapshot(StrictModel):
    captured_at: datetime
    repo_path: Path
    repo_state_path: Path | None = None
    branch: str = Field(min_length=1)
    head_commit: str = Field(min_length=1)
    status_lines: list[str] = Field(default_factory=list)
    local_branches: list[str] = Field(default_factory=list)
    worktrees: list[dict[str, str]] = Field(default_factory=list)
    repo_state_files: dict[str, str | None] = Field(default_factory=dict)
    recent_release_runs: list[str] = Field(default_factory=list)


class ReleasePlan(StrictModel):
    release_id: str = Field(min_length=1)
    active_objective: str = Field(min_length=1)
    current_tasks: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)

    @field_validator("current_tasks")
    @classmethod
    def current_tasks_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("current task IDs must not be empty")
        return values


class VerificationSpec(StrictModel):
    commands: list[str] = Field(default_factory=list)
    profile: str | None = None

    @field_validator("commands")
    @classmethod
    def commands_must_not_be_empty(cls, commands: list[str]) -> list[str]:
        if any(not command.strip() for command in commands):
            raise ValueError("verification commands must not be empty")
        return commands

    @model_validator(mode="after")
    def must_define_commands_or_profile(self) -> "VerificationSpec":
        if not self.commands and not self.profile:
            raise ValueError("verification must define commands or profile")
        return self


class RemoteDispatch(StrictModel):
    target: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    required_artifacts: list[str] = Field(default_factory=list)


class TaskContract(StrictModel):
    task_id: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    task_type: TaskType = TaskType.CODE_ONLY
    budget_class: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    allowed_files: list[str] = Field(min_length=1)
    forbidden_changes: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(min_length=1)
    verification: VerificationSpec
    stop_conditions: list[str] = Field(min_length=1)
    non_goals: list[str] = Field(default_factory=list)
    scientific_assumptions: list[str] = Field(default_factory=list)
    fixture_changes_allowed: bool = False
    tolerance_changes_allowed: bool = False
    benchmark_delta_required: bool = False
    remote_dispatch: RemoteDispatch | None = None
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("allowed_files", "required_evidence", "stop_conditions", "depends_on")
    @classmethod
    def list_items_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("list items must not be empty")
        return values

    @model_validator(mode="after")
    def scientific_tasks_require_assumptions(self) -> "TaskContract":
        if self.task_type in {TaskType.BENCHMARK, TaskType.SCIENTIFIC_VALIDATION}:
            if not self.scientific_assumptions:
                raise ValueError("benchmark and scientific validation tasks require assumptions")
        return self


class TaskState(StrEnum):
    PLANNED = "PLANNED"
    CONTRACT_WRITTEN = "CONTRACT_WRITTEN"
    WORKTREE_CREATED = "WORKTREE_CREATED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    ACCEPTED = "ACCEPTED"
    NEEDS_REVISION = "NEEDS_REVISION"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"


class CommandResult(StrictModel):
    command: str = Field(min_length=1)
    exit_code: int
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    duration_seconds: float = Field(ge=0)
    timed_out: bool = False


class ExecutorAttempt(StrictModel):
    attempt: int = Field(gt=0)
    backend: str = Field(min_length=1)
    model: str | None = None
    command: list[str] = Field(min_length=1)
    exit_code: int
    stdout_path: Path
    stderr_path: Path
    duration_seconds: float = Field(ge=0)
    timed_out: bool = False
    prompt_chars: int = Field(default=0, ge=0)
    stdout_chars: int = Field(default=0, ge=0)
    stderr_chars: int = Field(default=0, ge=0)


class ExecutorResult(StrictModel):
    command: list[str] = Field(min_length=1)
    exit_code: int
    stdout_path: Path
    stderr_path: Path
    duration_seconds: float = Field(ge=0)
    timed_out: bool = False
    backend: str = Field(min_length=1)
    model: str | None = None
    prompt_chars: int = Field(default=0, ge=0)
    stdout_chars: int = Field(default=0, ge=0)
    stderr_chars: int = Field(default=0, ge=0)
    attempts: list[ExecutorAttempt] = Field(default_factory=list)


class ContextSection(StrictModel):
    name: str = Field(min_length=1)
    source_path: Path
    content: str


class ContextBundle(StrictModel):
    sections: list[ContextSection] = Field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return sum(len(section.content) for section in self.sections)


class TaskRun(StrictModel):
    task_id: str = Field(min_length=1)
    state: TaskState
    worktree_path: Path
    branch: str = Field(min_length=1)
    executor_attempts: int = Field(ge=0)
    started_at: datetime
    updated_at: datetime
    changed_files: list[str] = Field(default_factory=list)
    diff_lines: int = Field(ge=0)
    verification_results: list[CommandResult] = Field(default_factory=list)


class EvidenceBundle(StrictModel):
    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    bundle_path: Path
    contract_path: Path
    run_state_path: Path
    executor_prompt_path: Path
    executor_stdout_path: Path
    executor_stderr_path: Path
    git_diff_path: Path
    changed_files_path: Path
    verification_log_path: Path
    model_call_metadata_path: Path | None = None
    executor_attempts_path: Path | None = None
    failure_diagnosis_path: Path | None = None
    scientific_review_path: Path | None = None
    benchmark_delta_path: Path | None = None
    remote_dispatch_path: Path | None = None
    review_path: Path | None = None
    decision_path: Path | None = None
    finalization_path: Path | None = None
    conflict_repair_path: Path | None = None
    soft_gate_decision_path: Path | None = None


class FailureDiagnosisInput(StrictModel):
    name: str = Field(min_length=1)
    value: str = Field(min_length=1)
    source: str | None = None


class FailureEvidenceExcerpt(StrictModel):
    source: str = Field(min_length=1)
    excerpt: str = Field(min_length=1)
    path: Path | None = None


class FailureDiagnosisAttempt(StrictModel):
    attempt: int = Field(gt=0)
    model: str | None = None
    exit_code: int
    timed_out: bool = False


class FailureDiagnosisSourceMetadata(StrictModel):
    backend: str = Field(min_length=1)
    model: str | None = None
    command: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    timed_out: bool = False
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    attempts: list[FailureDiagnosisAttempt] = Field(default_factory=list)

    @field_validator("command")
    @classmethod
    def command_must_not_be_empty(cls, command: list[str]) -> list[str]:
        if any(not value.strip() for value in command):
            raise ValueError("failure diagnosis command entries must not be empty")
        return command


class FailureDiagnosisGuidance(StrictModel):
    retryable: bool
    escalate: bool
    retry_reason: str | None = None
    escalate_reason: str | None = None


class FailureDiagnosis(StrictModel):
    diagnosis_inputs: list[FailureDiagnosisInput] = Field(default_factory=list)
    category: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    supporting_evidence_excerpts: list[FailureEvidenceExcerpt] = Field(default_factory=list)
    recommendation: str = Field(min_length=1)
    guidance: FailureDiagnosisGuidance
    source_metadata: FailureDiagnosisSourceMetadata


class ConflictRepairResult(StrictModel):
    attempted: bool = False
    conflicted_files: list[str] = Field(default_factory=list)
    prompt_path: Path | None = None
    executor_exit_code: int | None = None
    verification_exit_codes: list[int] = Field(default_factory=list)
    resolved: bool = False


class Decision(StrEnum):
    ACCEPTED = "accepted"
    NEEDS_REVISION = "needs_revision"
    FAILED = "failed"
    ESCALATED = "escalated"


class Reviewer(StrEnum):
    HUMAN = "human"
    STRONG_MODEL = "strong_model"
    DETERMINISTIC = "deterministic"
    HYBRID = "hybrid"


class ReviewDecision(StrictModel):
    task_id: str = Field(min_length=1)
    decision: Decision
    reviewer: Reviewer
    rationale: str = Field(min_length=1)
    risks: list[str] = Field(default_factory=list)
    follow_up_tasks: list[str] = Field(default_factory=list)
    soft_gate_findings: list["SoftGateFinding"] = Field(default_factory=list)


class SoftGateSeverity(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class SoftGateDecisionOutcome(StrEnum):
    ACCEPT = "accept"
    ACCEPT_WITH_MITIGATION = "accept_with_mitigation"
    DEFER = "defer"
    SPLIT_TASK = "split_task"
    ESCALATE = "escalate"
    REJECT = "reject"


class SoftGateFinding(StrictModel):
    finding_id: str = Field(min_length=1)
    severity: SoftGateSeverity
    risk: str = Field(min_length=1)
    recommended_actions: list[str] = Field(default_factory=list)
    evidence_paths: list[Path] = Field(default_factory=list)

    @field_validator("recommended_actions")
    @classmethod
    def recommended_actions_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("recommended actions must not be empty")
        return values


class SoftGateDecision(StrictModel):
    finding_id: str = Field(min_length=1)
    decision: SoftGateDecisionOutcome
    rationale: str = Field(min_length=1)
    fallback_plan: str = Field(min_length=1)
    validators_rerun: list[str] = Field(default_factory=list)
    evidence_paths: list[Path] = Field(default_factory=list)

    @field_validator("validators_rerun")
    @classmethod
    def validators_rerun_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("validators rerun entries must not be empty")
        return values


class TaskSoftGateDecisionRecord(StrictModel):
    task_id: str = Field(min_length=1)
    finding: SoftGateFinding
    decision: SoftGateDecision


class ReleaseSoftGateDecisionRecord(StrictModel):
    release_id: str = Field(min_length=1)
    decisions: list[TaskSoftGateDecisionRecord] = Field(default_factory=list)


class GeneratedContract(StrictModel):
    task_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    suggested_contract: TaskContract


class ContractPlan(StrictModel):
    release_id: str = Field(min_length=1)
    planner: str = Field(default="deterministic")
    generated_contracts: list[GeneratedContract] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    budget_ledger_path: Path | None = None
    planner_prompt_path: Path | None = None
    planner_stdout_path: Path | None = None
    planner_stderr_path: Path | None = None
    planner_metadata_path: Path | None = None
    state_review_snapshot_path: Path | None = None


class ContractNormalizationDecision(StrEnum):
    NORMALIZED = "normalized"
    REFUSED = "refused"


class ContractNormalizationRefusalReason(StrEnum):
    MISSING_REQUIRED_EVIDENCE = "missing_required_evidence"
    INVALID_VERIFICATION_COMMANDS = "invalid_verification_commands"
    OUT_OF_SCOPE_FILE_CHANGES = "out_of_scope_file_changes"
    AMBIGUOUS_CONTRACT_SEMANTICS = "ambiguous_contract_semantics"
    UNSAFE_NORMALIZATION = "unsafe_normalization"


class ContractNormalizationArtifactPaths(StrictModel):
    planner_prompt_path: Path | None = None
    planner_stdout_path: Path | None = None
    planner_stderr_path: Path | None = None
    planner_metadata_path: Path | None = None
    normalization_log_path: Path | None = None


class ContractNormalizationSnapshot(StrictModel):
    contract: TaskContract


class ContractNormalizationChangedField(StrictModel):
    path: str = Field(min_length=1)
    before: object | None = None
    after: object | None = None


class ContractNormalizationRequest(StrictModel):
    release_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    before_snapshot: ContractNormalizationSnapshot
    artifact_paths: ContractNormalizationArtifactPaths = Field(default_factory=ContractNormalizationArtifactPaths)


class ContractNormalizationOutcome(StrictModel):
    release_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    decision: ContractNormalizationDecision
    rationale: str = Field(min_length=1)
    before_snapshot: ContractNormalizationSnapshot
    after_snapshot: ContractNormalizationSnapshot | None = None
    changed_fields: list[ContractNormalizationChangedField] = Field(default_factory=list)
    refusal_reasons: list[ContractNormalizationRefusalReason] = Field(default_factory=list)
    artifact_paths: ContractNormalizationArtifactPaths = Field(default_factory=ContractNormalizationArtifactPaths)

class OverlapFinding(StrictModel):
    first_task_id: str = Field(min_length=1)
    second_task_id: str = Field(min_length=1)
    pattern: str = Field(min_length=1)
    severity: str = Field(min_length=1)


class ReleaseOverlapReport(StrictModel):
    findings: list[OverlapFinding] = Field(default_factory=list)

    @property
    def has_blocking_findings(self) -> bool:
        return any(finding.severity == "blocking" for finding in self.findings)

    @property
    def has_parallel_blockers(self) -> bool:
        return any(finding.severity in {"broad", "blocking"} for finding in self.findings)


class BudgetUsageEntry(StrictModel):
    name: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    configured: int | float | None = None
    actual: int | float = Field(ge=0)
    utilization: float | None = Field(default=None, ge=0)
    remaining: int | float | None = None
    over_by: int | float | None = None


class BudgetTaskSummary(StrictModel):
    task_id: str = Field(min_length=1)
    bundle_path: Path | None = None
    decision: str = Field(min_length=1)
    changed_files: int = Field(ge=0)
    diff_lines: int = Field(ge=0)
    context_chars: int = Field(ge=0)
    prompt_chars: int = Field(ge=0)
    output_chars: int = Field(ge=0)
    verification_command_count: int = Field(ge=0)
    verification_duration_seconds: float = Field(ge=0)
    executor_attempts: int = Field(ge=0)


class ModelAttemptSummary(StrictModel):
    model: str = Field(min_length=1)
    attempts: int = Field(ge=0)
    successful_attempts: int = Field(ge=0)
    failed_attempts: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    prompt_chars: int = Field(ge=0)
    stdout_chars: int = Field(ge=0)
    stderr_chars: int = Field(ge=0)


class BudgetFinding(StrictModel):
    kind: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    metric: str | None = None
    actual: int | float | None = None
    configured_limit: int | float | None = None
    share_of_limit: float | None = Field(default=None, ge=0)
    primary_model: str | None = None
    fallback_model: str | None = None
    verification_command_count: int | None = Field(default=None, ge=0)
    verification_duration_seconds: float | None = Field(default=None, ge=0)


class BudgetLedger(StrictModel):
    release_id: str = Field(min_length=1)
    budget: Budget
    usage: list[BudgetUsageEntry] = Field(default_factory=list)
    task_summaries: list[BudgetTaskSummary] = Field(default_factory=list)
    model_attempts: list[ModelAttemptSummary] = Field(default_factory=list)
    task_size_outliers: list[BudgetFinding] = Field(default_factory=list)
    verification_bottlenecks: list[BudgetFinding] = Field(default_factory=list)
    waste_signals: list[BudgetFinding] = Field(default_factory=list)


class BudgetTuningReport(StrictModel):
    release_id: str = Field(min_length=1)
    headline: str = Field(min_length=1)
    signals: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)

    def render_markdown(self) -> str:
        lines = [f"# {self.headline}", "", f"Release: `{self.release_id}`", ""]
        if self.signals:
            lines.extend(["## Signals", *[f"- {signal}" for signal in self.signals], ""])
        if self.recommendations:
            lines.extend(["## Guidance", *[f"- {item}" for item in self.recommendations], ""])
        return "\n".join(lines).rstrip() + "\n"
