from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator
import yaml

from agentic_devloop.yaml_io import dump_yaml_data

ModelT = TypeVar("ModelT", bound=BaseModel)


class CandidateEpic(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    rationale: str | None = None


class OutcomeReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_id: str = Field(min_length=1)
    task_id: str | None = Field(default=None, min_length=1)
    outcome: Literal["accepted", "needs_revision", "failed", "escalated"] | None = None
    run_summary_path: Path | None = None
    recorded_at: datetime | None = None


class FinalizationOutcomeReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_id: str = Field(min_length=1)
    outcome: Literal["accepted", "blocked", "failed", "needs_revision", "escalated"] | None = None
    run_summary_path: Path | None = None
    finalization_policy: str | None = Field(default=None, min_length=1)
    branch: str | None = Field(default=None, min_length=1)
    commit: str | None = Field(default=None, min_length=1)
    cleanup_report_path: Path | None = None
    blocked_reason: str | None = Field(default=None, min_length=1)
    blocked_type: str | None = Field(default=None, min_length=1)
    unresolved_finding_ids: list[str] = Field(default_factory=list)
    recommended_backlog_state: str | None = Field(default=None, min_length=1)
    recorded_at: datetime | None = None

    @field_validator("unresolved_finding_ids")
    @classmethod
    def _unresolved_finding_ids_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("unresolved finding IDs must not be empty")
        return values


class UnresolvedFindingReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    severity: Literal["low", "moderate", "high", "critical"] | None = None
    source_path: Path | None = None


class FinalReviewFollowUpMemoryReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_id: str = Field(min_length=1)
    finding_id: str = Field(min_length=1)
    classification: Literal[
        "accepted_risk",
        "backlog_follow_up",
        "duplicate",
        "false_positive",
        "verification_only",
        "scope_expansion",
    ]
    rationale_summary: str = Field(min_length=1)
    evidence_paths: list[Path] = Field(default_factory=list)
    fallback_plan: str | None = Field(default=None, min_length=1)
    validators_rerun: list[str] = Field(default_factory=list)
    adjudication_artifact_path: Path
    continuation_decision_path: Path
    recorded_at: datetime | None = None

    @field_validator("evidence_paths")
    @classmethod
    def _evidence_paths_must_not_be_empty(cls, values: list[Path]) -> list[Path]:
        if not values:
            raise ValueError("final review follow-up evidence paths must include at least one path")
        if any(not str(value).strip() for value in values):
            raise ValueError("final review follow-up evidence paths must not be empty")
        return values

    @field_validator("validators_rerun")
    @classmethod
    def _validators_rerun_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("validators rerun entries must not be empty")
        return values


class StateReviewSnapshotReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_path: Path
    captured_at: datetime
    release_id: str | None = Field(default=None, min_length=1)


class MetricsSnapshotReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metrics_path: Path
    recorded_at: datetime
    release_id: str | None = Field(default=None, min_length=1)


class TuningReportReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tuning_report_path: Path
    recorded_at: datetime
    release_id: str | None = Field(default=None, min_length=1)


class EpicMemoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    epic_id: str = Field(min_length=1)
    title: str | None = Field(default=None, min_length=1)
    rationale: str | None = Field(default=None, min_length=1)
    status_reason: str | None = Field(default=None, min_length=1)
    blocked_reason: str | None = Field(default=None, min_length=1)
    next_recommendations: list[str] = Field(default_factory=list)
    retry_count: int = Field(default=0, ge=0)
    repair_count: int = Field(default=0, ge=0)
    outcome_references: list[OutcomeReference] = Field(default_factory=list)
    finalization_outcome_references: list[FinalizationOutcomeReference] = Field(default_factory=list)
    unresolved_finding_references: list[UnresolvedFindingReference] = Field(default_factory=list)
    final_review_follow_up_memories: list[FinalReviewFollowUpMemoryReference] = Field(default_factory=list)
    state_review_snapshot_references: list[StateReviewSnapshotReference] = Field(default_factory=list)
    metrics_snapshot_references: list[MetricsSnapshotReference] = Field(default_factory=list)
    tuning_report_references: list[TuningReportReference] = Field(default_factory=list)
    updated_at: datetime | None = None


class EpicRefreshOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lifecycle_state: Literal["active", "completed", "blocked", "skipped", "reviewed"]
    status_reason: str | None = Field(default=None, min_length=1)
    blocked_reason: str | None = Field(default=None, min_length=1)
    retry_count: int | None = Field(default=None, ge=0)
    repair_count: int | None = Field(default=None, ge=0)
    next_recommendations: list[str] = Field(default_factory=list)
    outcome_references: list[OutcomeReference] = Field(default_factory=list)
    finalization_outcome_references: list[FinalizationOutcomeReference] = Field(default_factory=list)
    unresolved_finding_references: list[UnresolvedFindingReference] = Field(default_factory=list)
    final_review_follow_up_memories: list[FinalReviewFollowUpMemoryReference] = Field(default_factory=list)
    state_review_snapshot_references: list[StateReviewSnapshotReference] = Field(default_factory=list)
    metrics_snapshot_references: list[MetricsSnapshotReference] = Field(default_factory=list)
    tuning_report_references: list[TuningReportReference] = Field(default_factory=list)

    @field_validator("next_recommendations")
    @classmethod
    def _next_recommendations_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("next recommendations must not be empty")
        return values


class RecentRunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_summary_path: Path
    release_id: str | None = None
    task_id: str | None = None
    outcome: str | None = None
    recorded_at: datetime


class BacklogState(BaseModel):
    model_config = ConfigDict(extra="allow")

    active_goal: str | None = None
    governor_mode: str | None = None
    current_focus: str | None = None
    active_epic: str | None = None
    completed_epics: list[str] = Field(default_factory=list)
    blocked_epics: list[str] = Field(default_factory=list)
    candidate_epics: list[CandidateEpic] = Field(default_factory=list)
    recent_run_summaries: list[RecentRunSummary] = Field(default_factory=list)
    active_epics: list[EpicMemoryRecord] = Field(default_factory=list)
    reviewed_epics: list[EpicMemoryRecord] = Field(default_factory=list)
    completed_epic_records: list[EpicMemoryRecord] = Field(default_factory=list)
    skipped_epics: list[EpicMemoryRecord] = Field(default_factory=list)
    blocked_epic_records: list[EpicMemoryRecord] = Field(default_factory=list)
    last_reviewed: date | None = None
    notes: list[str] = Field(default_factory=list)

    @field_validator("completed_epics", "blocked_epics", "notes")
    @classmethod
    def _list_items_must_not_be_empty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("list items must not be empty")
        return values


class StateStore:
    def __init__(self, backlog_state_path: Path) -> None:
        self.backlog_state_path = backlog_state_path

    def load(self) -> BacklogState:
        if not self.backlog_state_path.exists():
            return BacklogState()

        data = self.backlog_state_path.read_text(encoding="utf-8")
        parsed = {} if not data.strip() else yaml.safe_load(data)
        if parsed is None:
            parsed = {}
        if not isinstance(parsed, dict):
            raise ValueError(f"backlog state must be a mapping: {self.backlog_state_path}")
        parsed = self._normalize_legacy_backlog_state(parsed)

        return BacklogState.model_validate(parsed)

    def save(self, state: BacklogState) -> Path:
        self.backlog_state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = state.model_dump(mode="json", exclude_none=True)
        self.backlog_state_path.write_text(dump_yaml_data(payload), encoding="utf-8")
        return self.backlog_state_path

    def mark_active_epic(self, epic_id: str) -> BacklogState:
        state = self.load()
        record = self._pop_existing_epic_record(state, epic_id) or EpicMemoryRecord(epic_id=epic_id)
        record.updated_at = datetime.now(UTC)
        state.active_epic = epic_id
        state.completed_epics = [value for value in state.completed_epics if value != epic_id]
        state.blocked_epics = [value for value in state.blocked_epics if value != epic_id]
        state.completed_epic_records = [value for value in state.completed_epic_records if value.epic_id != epic_id]
        state.blocked_epic_records = [value for value in state.blocked_epic_records if value.epic_id != epic_id]
        state.skipped_epics = [value for value in state.skipped_epics if value.epic_id != epic_id]
        self._set_record(state.active_epics, record)
        self.save(state)
        return state

    def mark_completed_epic(self, epic_id: str) -> BacklogState:
        state = self.load()
        record = self._pop_existing_epic_record(state, epic_id) or EpicMemoryRecord(epic_id=epic_id)
        record.updated_at = datetime.now(UTC)
        if state.active_epic == epic_id:
            state.active_epic = None
        if epic_id not in state.completed_epics:
            state.completed_epics.append(epic_id)
        state.blocked_epics = [value for value in state.blocked_epics if value != epic_id]
        state.active_epics = [value for value in state.active_epics if value.epic_id != epic_id]
        state.blocked_epic_records = [value for value in state.blocked_epic_records if value.epic_id != epic_id]
        state.skipped_epics = [value for value in state.skipped_epics if value.epic_id != epic_id]
        self._set_record(state.completed_epic_records, record)
        self.save(state)
        return state

    def mark_reviewed_epic(
        self,
        epic_id: str,
        *,
        status_reason: str | None = None,
    ) -> BacklogState:
        state = self.load()
        record = self._pop_existing_epic_record(state, epic_id) or EpicMemoryRecord(epic_id=epic_id)
        record.status_reason = status_reason
        record.updated_at = datetime.now(UTC)
        self._set_record(state.reviewed_epics, record)
        self.save(state)
        return state

    def mark_skipped_epic(
        self,
        epic_id: str,
        *,
        status_reason: str,
    ) -> BacklogState:
        state = self.load()
        record = self._pop_existing_epic_record(state, epic_id) or EpicMemoryRecord(epic_id=epic_id)
        record.status_reason = status_reason
        record.updated_at = datetime.now(UTC)
        if state.active_epic == epic_id:
            state.active_epic = None
        state.active_epics = [value for value in state.active_epics if value.epic_id != epic_id]
        state.completed_epics = [value for value in state.completed_epics if value != epic_id]
        state.blocked_epics = [value for value in state.blocked_epics if value != epic_id]
        state.completed_epic_records = [value for value in state.completed_epic_records if value.epic_id != epic_id]
        state.blocked_epic_records = [value for value in state.blocked_epic_records if value.epic_id != epic_id]
        self._set_record(state.skipped_epics, record)
        self.save(state)
        return state

    def mark_blocked_epic(self, epic_id: str, *, blocked_reason: str | None = None) -> BacklogState:
        state = self.load()
        record = self._pop_existing_epic_record(state, epic_id) or EpicMemoryRecord(epic_id=epic_id)
        record.blocked_reason = blocked_reason
        record.updated_at = datetime.now(UTC)
        if state.active_epic == epic_id:
            state.active_epic = None
        if epic_id not in state.blocked_epics:
            state.blocked_epics.append(epic_id)
        state.completed_epics = [value for value in state.completed_epics if value != epic_id]
        state.active_epics = [value for value in state.active_epics if value.epic_id != epic_id]
        state.completed_epic_records = [value for value in state.completed_epic_records if value.epic_id != epic_id]
        state.skipped_epics = [value for value in state.skipped_epics if value.epic_id != epic_id]
        self._set_record(state.blocked_epic_records, record)
        self.save(state)
        return state

    def increment_epic_retry_count(self, epic_id: str, *, amount: int = 1) -> BacklogState:
        return self._increment_epic_counter(epic_id, amount=amount, counter="retry_count")

    def increment_epic_repair_count(self, epic_id: str, *, amount: int = 1) -> BacklogState:
        return self._increment_epic_counter(epic_id, amount=amount, counter="repair_count")

    def apply_epic_refresh_outcome(self, epic_id: str, outcome: EpicRefreshOutcome) -> BacklogState:
        state = self.load()
        record = self._pop_existing_epic_record(state, epic_id) or EpicMemoryRecord(epic_id=epic_id)
        record.status_reason = outcome.status_reason
        record.blocked_reason = outcome.blocked_reason
        if outcome.retry_count is not None:
            record.retry_count = outcome.retry_count
        if outcome.repair_count is not None:
            record.repair_count = outcome.repair_count
        if outcome.next_recommendations:
            record.next_recommendations = list(outcome.next_recommendations)
        record.outcome_references = self._merge_unique_references(
            record.outcome_references,
            outcome.outcome_references,
        )
        record.finalization_outcome_references = self._merge_unique_references(
            record.finalization_outcome_references,
            outcome.finalization_outcome_references,
        )
        record.unresolved_finding_references = self._merge_unique_references(
            record.unresolved_finding_references,
            outcome.unresolved_finding_references,
        )
        record.final_review_follow_up_memories = self._merge_unique_references(
            record.final_review_follow_up_memories,
            outcome.final_review_follow_up_memories,
        )
        record.state_review_snapshot_references = self._merge_unique_references(
            record.state_review_snapshot_references,
            outcome.state_review_snapshot_references,
        )
        record.metrics_snapshot_references = self._merge_unique_references(
            record.metrics_snapshot_references,
            outcome.metrics_snapshot_references,
        )
        record.tuning_report_references = self._merge_unique_references(
            record.tuning_report_references,
            outcome.tuning_report_references,
        )
        record.updated_at = datetime.now(UTC)
        self._set_epic_lifecycle_state(state, record, outcome.lifecycle_state)
        self.save(state)
        return state

    def add_epic_outcome_reference(self, epic_id: str, reference: OutcomeReference) -> BacklogState:
        state = self.load()
        record = self._get_or_create_epic_record(state, epic_id)
        record.outcome_references.append(reference)
        record.updated_at = datetime.now(UTC)
        self.save(state)
        return state

    def add_epic_unresolved_finding_reference(
        self,
        epic_id: str,
        reference: UnresolvedFindingReference,
    ) -> BacklogState:
        state = self.load()
        record = self._get_or_create_epic_record(state, epic_id)
        record.unresolved_finding_references.append(reference)
        record.updated_at = datetime.now(UTC)
        self.save(state)
        return state

    def add_epic_final_review_follow_up_memory(
        self,
        epic_id: str,
        reference: FinalReviewFollowUpMemoryReference,
    ) -> BacklogState:
        state = self.load()
        record = self._get_or_create_epic_record(state, epic_id)
        record.final_review_follow_up_memories = self._merge_unique_references(
            record.final_review_follow_up_memories,
            [reference],
        )
        record.updated_at = datetime.now(UTC)
        self.save(state)
        return state

    def add_epic_finalization_outcome_reference(
        self,
        epic_id: str,
        reference: FinalizationOutcomeReference,
    ) -> BacklogState:
        state = self.load()
        record = self._get_or_create_epic_record(state, epic_id)
        record.finalization_outcome_references.append(reference)
        record.updated_at = datetime.now(UTC)
        self.save(state)
        return state

    def add_state_review_snapshot_reference(
        self,
        epic_id: str,
        reference: StateReviewSnapshotReference,
    ) -> BacklogState:
        state = self.load()
        record = self._get_or_create_epic_record(state, epic_id)
        record.state_review_snapshot_references.append(reference)
        record.updated_at = datetime.now(UTC)
        self.save(state)
        return state

    def record_recent_run_summary(
        self,
        run_summary_path: Path,
        *,
        release_id: str | None = None,
        task_id: str | None = None,
        outcome: str | None = None,
        recorded_at: datetime | None = None,
        max_entries: int = 20,
    ) -> BacklogState:
        if max_entries <= 0:
            raise ValueError("max_entries must be greater than 0")

        state = self.load()
        now = recorded_at or datetime.now(UTC)
        normalized_path = Path(str(run_summary_path))

        state.recent_run_summaries = [
            summary
            for summary in state.recent_run_summaries
            if summary.run_summary_path != normalized_path
        ]
        state.recent_run_summaries.insert(
            0,
            RecentRunSummary(
                run_summary_path=normalized_path,
                release_id=release_id,
                task_id=task_id,
                outcome=outcome,
                recorded_at=now,
            ),
        )
        state.recent_run_summaries = state.recent_run_summaries[:max_entries]
        self.save(state)
        return state

    def _increment_epic_counter(
        self,
        epic_id: str,
        *,
        amount: int,
        counter: Literal["retry_count", "repair_count"],
    ) -> BacklogState:
        if amount <= 0:
            raise ValueError("amount must be greater than 0")
        if counter not in {"retry_count", "repair_count"}:
            raise ValueError(f"unsupported epic counter: {counter}")
        state = self.load()
        record = self._get_or_create_epic_record(state, epic_id)
        setattr(record, counter, getattr(record, counter) + amount)
        record.updated_at = datetime.now(UTC)
        self.save(state)
        return state

    def _get_or_create_epic_record(self, state: BacklogState, epic_id: str) -> EpicMemoryRecord:
        for records in self._epic_record_lists(state):
            for record in records:
                if record.epic_id == epic_id:
                    return record
        record = EpicMemoryRecord(epic_id=epic_id, updated_at=datetime.now(UTC))
        target_records = state.active_epics
        if epic_id in state.completed_epics:
            target_records = state.completed_epic_records
        elif epic_id in state.blocked_epics:
            target_records = state.blocked_epic_records
        elif state.active_epic == epic_id:
            target_records = state.active_epics
        target_records.insert(0, record)
        return record

    def _set_epic_lifecycle_state(
        self,
        state: BacklogState,
        record: EpicMemoryRecord,
        lifecycle_state: Literal["active", "completed", "blocked", "skipped", "reviewed"],
    ) -> None:
        epic_id = record.epic_id
        state.active_epics = [value for value in state.active_epics if value.epic_id != epic_id]
        state.completed_epic_records = [value for value in state.completed_epic_records if value.epic_id != epic_id]
        state.blocked_epic_records = [value for value in state.blocked_epic_records if value.epic_id != epic_id]
        state.skipped_epics = [value for value in state.skipped_epics if value.epic_id != epic_id]
        state.reviewed_epics = [value for value in state.reviewed_epics if value.epic_id != epic_id]
        state.completed_epics = [value for value in state.completed_epics if value != epic_id]
        state.blocked_epics = [value for value in state.blocked_epics if value != epic_id]
        if state.active_epic == epic_id:
            state.active_epic = None

        if lifecycle_state == "active":
            state.active_epic = epic_id
            self._set_record(state.active_epics, record)
            return
        if lifecycle_state == "completed":
            if epic_id not in state.completed_epics:
                state.completed_epics.append(epic_id)
            self._set_record(state.completed_epic_records, record)
            return
        if lifecycle_state == "blocked":
            if epic_id not in state.blocked_epics:
                state.blocked_epics.append(epic_id)
            self._set_record(state.blocked_epic_records, record)
            return
        if lifecycle_state == "skipped":
            self._set_record(state.skipped_epics, record)
            return
        self._set_record(state.reviewed_epics, record)

    def _pop_existing_epic_record(self, state: BacklogState, epic_id: str) -> EpicMemoryRecord | None:
        for records in self._epic_record_lists(state):
            for index, record in enumerate(records):
                if record.epic_id == epic_id:
                    return records.pop(index)
        return None

    @staticmethod
    def _epic_record_lists(state: BacklogState) -> tuple[list[EpicMemoryRecord], ...]:
        return (
            state.active_epics,
            state.reviewed_epics,
            state.completed_epic_records,
            state.blocked_epic_records,
            state.skipped_epics,
        )

    @staticmethod
    def _set_record(records: list[EpicMemoryRecord], record: EpicMemoryRecord) -> None:
        records[:] = [value for value in records if value.epic_id != record.epic_id]
        records.insert(0, record)

    @staticmethod
    def _merge_unique_references(existing: list[ModelT], additions: list[ModelT]) -> list[ModelT]:
        merged = list(existing)
        seen = {item.model_dump_json(exclude_none=True) for item in merged}
        for item in additions:
            key = item.model_dump_json(exclude_none=True)
            if key in seen:
                continue
            merged.append(item)
            seen.add(key)
        return merged

    @staticmethod
    def _normalize_legacy_backlog_state(parsed: dict[object, object]) -> dict[object, object]:
        normalized = dict(parsed)
        legacy_record_keys = (
            "active_epics",
            "reviewed_epics",
            "completed_epic_records",
            "skipped_epics",
            "blocked_epic_records",
        )
        for key in legacy_record_keys:
            value = normalized.get(key)
            if not isinstance(value, list):
                continue
            normalized[key] = [
                {"epic_id": item} if isinstance(item, str) else item
                for item in value
            ]
        return normalized
