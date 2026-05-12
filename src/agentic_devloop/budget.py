from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agentic_devloop.models import Budget


@dataclass(frozen=True)
class BudgetLedgerEntry:
    release_id: str
    kind: str
    model: str
    reason: str
    created_at: str


def reserve_strong_model_call(
    *,
    runs_dir: Path,
    release_id: str,
    budget: Budget,
    model: str,
    reason: str,
    now: datetime | None = None,
) -> Path:
    ledger_path = runs_dir / release_id / "budget_ledger.json"
    entries = _read_entries(ledger_path)
    used = sum(1 for entry in entries if entry.get("kind") == "strong_model")
    if used >= budget.max_strong_model_calls_per_release:
        raise ValueError(
            "strong-model call budget exceeded: "
            f"{used}/{budget.max_strong_model_calls_per_release} already used for {release_id}"
        )

    created_at = (now or datetime.now(UTC)).isoformat()
    entry = BudgetLedgerEntry(
        release_id=release_id,
        kind="strong_model",
        model=model,
        reason=reason,
        created_at=created_at,
    )
    entries.append(entry.__dict__)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    return ledger_path


def _read_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))
