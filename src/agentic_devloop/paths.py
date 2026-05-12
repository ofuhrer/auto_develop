from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GovernorRunPaths:
    run_root: Path
    log_path: Path
    raw_log_path: Path
    events_path: Path


def governor_run_paths(*, runs_dir: Path, run_id: str) -> GovernorRunPaths:
    run_root = runs_dir / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    return GovernorRunPaths(
        run_root=run_root,
        log_path=run_root / "governor.log",
        raw_log_path=run_root / "governor.raw.log",
        events_path=run_root / "events.jsonl",
    )
