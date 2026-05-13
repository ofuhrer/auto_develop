from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Callable

from agentic_devloop.paths import GovernorRunPaths, governor_run_paths


class GovernorEventType(StrEnum):
    GOVERNOR_STARTED = "governor_started"
    STATE_REVIEW_COMPLETED = "state_review_completed"
    STATE_REFRESH_SUMMARY = "state_refresh_summary"
    STATE_REFRESH_ERROR = "state_refresh_error"
    BACKLOG_SELECTION_COMPLETED = "backlog_selection_completed"
    BACKLOG_PLANNING_COMPLETED = "backlog_planning_completed"
    EPIC_SELECTED = "epic_selected"
    OBJECTIVE_GENERATION_COMPLETED = "objective_generation_completed"
    OBJECTIVE_READY = "objective_ready"
    CONTRACT_GENERATION_COMPLETED = "contract_generation_completed"
    CONTRACT_PLAN_COMPLETED = "contract_plan_completed"
    CONTRACT_NORMALIZATION = "contract_normalization"
    CHILD_RELEASE_STARTED = "child_release_started"
    CHILD_RELEASE_COMPLETED = "child_release_completed"
    RELEASE_STARTED = "release_started"
    RELEASE_COMPLETED = "release_completed"
    FEATURE_REVIEW_COMPLETED = "feature_review_completed"
    REPAIR_DECISION = "repair_decision"
    FINAL_VERIFICATION_COMPLETED = "final_verification_completed"
    FINALIZATION_DECISION = "finalization_decision"
    FINALIZATION_COMPLETED = "finalization_completed"
    CLEANUP_ELIGIBILITY_EVALUATED = "cleanup_eligibility_evaluated"
    STATE_REFRESHED = "state_refreshed"
    NEXT_EPIC_SELECTED = "next_epic_selected"
    STOP_REASON_RECORDED = "stop_reason_recorded"
    GOVERNOR_COMPLETED = "governor_completed"


@dataclass(frozen=True)
class GovernorEventContext:
    phase: str
    release_id: str | None = None
    epic_id: str | None = None
    task_id: str | None = None
    decision: str | None = None
    outcome: str | None = None
    stop_reason: str | None = None
    cycle_index: int | None = None
    artifact_count: int | None = None
    details: dict[str, str | int | float | bool] | None = None

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {"phase": self.phase}
        if self.release_id is not None:
            payload["release_id"] = self.release_id
        if self.epic_id is not None:
            payload["epic_id"] = self.epic_id
        if self.task_id is not None:
            payload["task_id"] = self.task_id
        if self.decision is not None:
            payload["decision"] = self.decision
        if self.outcome is not None:
            payload["outcome"] = self.outcome
        if self.stop_reason is not None:
            payload["stop_reason"] = self.stop_reason
        if self.cycle_index is not None:
            payload["cycle_index"] = self.cycle_index
        if self.artifact_count is not None:
            payload["artifact_count"] = self.artifact_count
        if self.details is not None:
            payload["details"] = self.details
        return payload

    def to_compact_log(self) -> str:
        parts = [f"phase={self.phase}"]
        if self.release_id is not None:
            parts.append(f"release_id={self.release_id}")
        if self.epic_id is not None:
            parts.append(f"epic_id={self.epic_id}")
        if self.task_id is not None:
            parts.append(f"task_id={self.task_id}")
        if self.decision is not None:
            parts.append(f"decision={self.decision}")
        if self.outcome is not None:
            parts.append(f"outcome={self.outcome}")
        if self.stop_reason is not None:
            parts.append(f"stop_reason={self.stop_reason}")
        if self.cycle_index is not None:
            parts.append(f"cycle_index={self.cycle_index}")
        if self.artifact_count is not None:
            parts.append(f"artifact_count={self.artifact_count}")
        if self.details:
            detail_value = ",".join(f"{key}:{value}" for key, value in sorted(self.details.items()))
            parts.append(f"details={detail_value}")
        return " ".join(parts)


@dataclass(frozen=True)
class GovernorEvent:
    timestamp: str
    event_type: GovernorEventType
    message: str
    artifacts: tuple[str, ...]
    context: GovernorEventContext


class GovernorEventLogWriter:
    def __init__(
        self,
        *,
        paths: GovernorRunPaths,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self._paths = paths
        self._progress = progress
        self._lock = threading.Lock()

    @property
    def paths(self) -> GovernorRunPaths:
        return self._paths

    def write(
        self,
        *,
        event_type: GovernorEventType,
        message: str,
        artifacts: list[Path] | tuple[Path, ...] = (),
        context: GovernorEventContext | None = None,
    ) -> GovernorEvent:
        timestamp = datetime.now(UTC).isoformat()
        artifact_links = tuple(str(path) for path in artifacts)
        event_context = context or GovernorEventContext(phase=event_type.value, artifact_count=len(artifact_links))
        event = GovernorEvent(
            timestamp=timestamp,
            event_type=event_type,
            message=message,
            artifacts=artifact_links,
            context=event_context,
        )
        compact_context = event_context.to_compact_log()
        with self._lock:
            self._paths.run_root.mkdir(parents=True, exist_ok=True)
            with self._paths.raw_log_path.open("a", encoding="utf-8") as raw_file:
                raw_file.write(
                    f"{timestamp} event={event_type} message={message} context={compact_context}\n"
                )
            with self._paths.log_path.open("a", encoding="utf-8") as human_file:
                human_file.write(f"{timestamp} [{event_type}] {message} ({compact_context})\n")
            with self._paths.events_path.open("a", encoding="utf-8") as events_file:
                events_file.write(
                    json.dumps(
                        {
                            "timestamp": event.timestamp,
                            "event_type": event.event_type,
                            "message": event.message,
                            "artifacts": list(event.artifacts),
                            "context": event.context.to_json(),
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
        if self._progress is not None:
            self._progress(message)
        return event


def build_governor_event_log_writer(
    *,
    runs_dir: Path,
    run_id: str,
    progress: Callable[[str], None] | None = None,
) -> GovernorEventLogWriter:
    return GovernorEventLogWriter(
        paths=governor_run_paths(runs_dir=runs_dir, run_id=run_id),
        progress=progress,
    )
