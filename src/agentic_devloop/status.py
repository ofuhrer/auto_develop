from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    task_id: str
    decision: str
    bundle_path: Path


def load_run_summaries(runs_dir: Path = Path("runs"), limit: int = 10) -> list[RunSummary]:
    if not runs_dir.exists():
        return []

    summaries: list[RunSummary] = []
    for decision_path in sorted(runs_dir.glob("*/*/evidence/decision.yaml"), reverse=True):
        data = yaml.safe_load(decision_path.read_text(encoding="utf-8")) or {}
        task_id = str(data.get("task_id", decision_path.parents[1].name))
        summaries.append(
            RunSummary(
                run_id=decision_path.parents[2].name,
                task_id=task_id,
                decision=str(data.get("decision", "unknown")),
                bundle_path=decision_path.parent,
            )
        )
        if len(summaries) >= limit:
            break

    return summaries
