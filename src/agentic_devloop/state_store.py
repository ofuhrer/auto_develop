from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
import yaml

from agentic_devloop.yaml_io import dump_yaml_data


class CandidateEpic(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    rationale: str | None = None


class OutcomeReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_id: str = Field(min_length=1)
    task_id: str | None = Field(default=None, min_length=1)
    outcome: str | None = Field(default=None, min_length=1)
    run_summary_path: Path | None = None
    recorded_at: datetime | None = None


class UnresolvedFindingReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    severity: str | None = Field(default=None, min_length=1)
    source_path: Path | None = None


class StateReviewSnapshotReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_path: Path
    captured_at: datetime
    release_id: str | None = Field(default=None, min_length=1)


class EpicMemoryRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    epic_id: str = Field(min_length=1)
    title: str | None = Field(default=None, min_length=1)
    rationale: str | None = Field(default=None, min_length=1)
    status_reason: str | None = Field(default=None, min_length=1)
    blocked_reason: str | None = Field(default=None, min_length=1)
    retry_count: int = Field(default=0, ge=0)
    repair_count: int = Field(default=0, ge=0)
    outcome_references: list[OutcomeReference] = Field(default_factory=list)
    unresolved_finding_references: list[UnresolvedFindingReference] = Field(default_factory=list)
    state_review_snapshot_references: list[StateReviewSnapshotReference] = Field(default_factory=list)
    updated_at: datetime | None = None


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
        state.active_epics.insert(0, record)
        return record

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
