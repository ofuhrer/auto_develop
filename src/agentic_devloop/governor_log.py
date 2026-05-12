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
    EPIC_SELECTED = "epic_selected"
    RELEASE_STARTED = "release_started"
    RELEASE_COMPLETED = "release_completed"
    REPAIR_DECISION = "repair_decision"
    STATE_REFRESHED = "state_refreshed"
    GOVERNOR_COMPLETED = "governor_completed"


@dataclass(frozen=True)
class GovernorEvent:
    timestamp: str
    event_type: GovernorEventType
    message: str
    artifacts: tuple[str, ...]


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
    ) -> GovernorEvent:
        timestamp = datetime.now(UTC).isoformat()
        artifact_links = tuple(str(path) for path in artifacts)
        event = GovernorEvent(
            timestamp=timestamp,
            event_type=event_type,
            message=message,
            artifacts=artifact_links,
        )
        with self._lock:
            self._paths.run_root.mkdir(parents=True, exist_ok=True)
            with self._paths.raw_log_path.open("a", encoding="utf-8") as raw_file:
                raw_file.write(f"{timestamp} event={event_type} message={message}\n")
            with self._paths.log_path.open("a", encoding="utf-8") as human_file:
                human_file.write(f"{timestamp} [{event_type}] {message}\n")
            with self._paths.events_path.open("a", encoding="utf-8") as events_file:
                events_file.write(
                    json.dumps(
                        {
                            "timestamp": event.timestamp,
                            "event_type": event.event_type,
                            "message": event.message,
                            "artifacts": list(event.artifacts),
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
