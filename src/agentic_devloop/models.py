from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecutorConfig(StrictModel):
    type: str = Field(min_length=1)
    model: str = Field(min_length=1)
    max_walltime_minutes: int = Field(gt=0)


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


class ProjectConfig(StrictModel):
    project_id: str = Field(min_length=1)
    repo_path: Path
    default_base_branch: str = Field(min_length=1)
    worktree_root: Path
    executor: ExecutorConfig
    verification_profiles: dict[str, VerificationProfile] = Field(min_length=1)
    budget: Budget


class ReleaseObjective(StrictModel):
    release_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    non_goals: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(min_length=1)


class VerificationSpec(StrictModel):
    commands: list[str] = Field(min_length=1)

    @field_validator("commands")
    @classmethod
    def commands_must_not_be_empty(cls, commands: list[str]) -> list[str]:
        if any(not command.strip() for command in commands):
            raise ValueError("verification commands must not be empty")
        return commands


class TaskContract(StrictModel):
    task_id: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    budget_class: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    allowed_files: list[str] = Field(min_length=1)
    forbidden_changes: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(min_length=1)
    verification: VerificationSpec
    stop_conditions: list[str] = Field(min_length=1)
    non_goals: list[str] = Field(default_factory=list)
    scientific_assumptions: list[str] = Field(default_factory=list)

    @field_validator("allowed_files", "required_evidence", "stop_conditions")
    @classmethod
    def list_items_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("list items must not be empty")
        return values


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


class ExecutorResult(StrictModel):
    command: list[str] = Field(min_length=1)
    exit_code: int
    stdout_path: Path
    stderr_path: Path
    duration_seconds: float = Field(ge=0)
    timed_out: bool = False
    backend: str = Field(min_length=1)
    model: str | None = None


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
    review_path: Path | None = None
    decision_path: Path | None = None


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
