from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from agentic_devloop.runtime_state import read_json


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
    seen: set[tuple[str, str]] = set()
    for decision_path in sorted(runs_dir.glob("*/*/evidence/decision.yaml"), reverse=True):
        data = yaml.safe_load(decision_path.read_text(encoding="utf-8")) or {}
        task_id = str(data.get("task_id", decision_path.parents[1].name))
        seen.add((decision_path.parents[2].name, task_id))
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

    if len(summaries) < limit:
        for state_path in sorted(runs_dir.glob("*/*/run_state.json"), reverse=True):
            state = read_json(state_path)
            if not state:
                continue
            run_id = state_path.parents[1].name
            task_id = str(state.get("task_id", state_path.parent.name))
            if (run_id, task_id) in seen:
                continue
            summaries.append(
                RunSummary(
                    run_id=run_id,
                    task_id=task_id,
                    decision=str(state.get("decision") or state.get("state", "unknown")).lower(),
                    bundle_path=state_path.parent / "evidence",
                )
            )
            if len(summaries) >= limit:
                break

    return summaries
