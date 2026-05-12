from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator
import yaml

from agentic_devloop.yaml_io import dump_yaml_data


class CandidateEpic(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    rationale: str | None = None


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

        return BacklogState.model_validate(parsed)

    def save(self, state: BacklogState) -> Path:
        self.backlog_state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = state.model_dump(mode="json", exclude_none=True)
        self.backlog_state_path.write_text(dump_yaml_data(payload), encoding="utf-8")
        return self.backlog_state_path

    def mark_active_epic(self, epic_id: str) -> BacklogState:
        state = self.load()
        state.active_epic = epic_id
        state.completed_epics = [value for value in state.completed_epics if value != epic_id]
        state.blocked_epics = [value for value in state.blocked_epics if value != epic_id]
        self.save(state)
        return state

    def mark_completed_epic(self, epic_id: str) -> BacklogState:
        state = self.load()
        if state.active_epic == epic_id:
            state.active_epic = None
        if epic_id not in state.completed_epics:
            state.completed_epics.append(epic_id)
        state.blocked_epics = [value for value in state.blocked_epics if value != epic_id]
        self.save(state)
        return state

    def mark_blocked_epic(self, epic_id: str) -> BacklogState:
        state = self.load()
        if state.active_epic == epic_id:
            state.active_epic = None
        if epic_id not in state.blocked_epics:
            state.blocked_epics.append(epic_id)
        state.completed_epics = [value for value in state.completed_epics if value != epic_id]
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
