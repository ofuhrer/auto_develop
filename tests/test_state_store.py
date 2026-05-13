from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentic_devloop.state_store import (
    BacklogState,
    OutcomeReference,
    StateReviewSnapshotReference,
    StateStore,
    UnresolvedFindingReference,
)


def test_load_existing_backlog_state_yaml_with_compatibility_fields(tmp_path: Path) -> None:
    state_path = tmp_path / "repo_state" / "demo" / "backlog_state.yaml"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        """
active_goal: autonomous-goal
active_epic: epic-2
completed_epics:
  - epic-1
blocked_epics: []
candidate_epics:
  - id: epic-2
    title: Epic Two
    rationale: Selected next
last_reviewed: 2026-05-12
notes:
  - keep momentum
custom_field: preserved
""".strip()
        + "\n",
        encoding="utf-8",
    )

    store = StateStore(state_path)
    state = store.load()

    assert state.active_goal == "autonomous-goal"
    assert state.active_epic == "epic-2"
    assert state.completed_epics == ["epic-1"]
    assert state.candidate_epics[0].id == "epic-2"
    assert state.model_extra == {"custom_field": "preserved"}


def test_mark_active_completed_and_blocked_epic_transitions(tmp_path: Path) -> None:
    state_path = tmp_path / "repo_state" / "demo" / "backlog_state.yaml"
    store = StateStore(state_path)
    store.save(
        BacklogState(
            active_epic="epic-1",
            completed_epics=["epic-2"],
            blocked_epics=["epic-3"],
        )
    )

    active = store.mark_active_epic("epic-2")
    assert active.active_epic == "epic-2"
    assert active.completed_epics == []

    completed = store.mark_completed_epic("epic-2")
    assert completed.active_epic is None
    assert completed.completed_epics == ["epic-2"]
    assert "epic-2" not in completed.blocked_epics

    blocked = store.mark_blocked_epic("epic-2")
    assert blocked.active_epic is None
    assert blocked.blocked_epics == ["epic-3", "epic-2"]
    assert "epic-2" not in blocked.completed_epics


def test_mark_reviewed_and_skipped_epic_transitions(tmp_path: Path) -> None:
    state_path = tmp_path / "repo_state" / "demo" / "backlog_state.yaml"
    store = StateStore(state_path)
    store.mark_active_epic("epic-1")

    reviewed = store.mark_reviewed_epic("epic-1", status_reason="state-review-complete")
    assert reviewed.reviewed_epics[0].epic_id == "epic-1"
    assert reviewed.reviewed_epics[0].status_reason == "state-review-complete"

    skipped = store.mark_skipped_epic("epic-1", status_reason="out-of-scope-for-release")
    assert skipped.active_epic is None
    assert skipped.skipped_epics[0].epic_id == "epic-1"
    assert skipped.skipped_epics[0].status_reason == "out-of-scope-for-release"
    assert skipped.completed_epics == []
    assert skipped.blocked_epics == []


def test_epic_memory_references_and_counters(tmp_path: Path) -> None:
    state_path = tmp_path / "repo_state" / "demo" / "backlog_state.yaml"
    store = StateStore(state_path)

    state = store.increment_epic_retry_count("epic-1")
    state = store.increment_epic_repair_count("epic-1", amount=2)
    state = store.add_epic_outcome_reference(
        "epic-1",
        OutcomeReference(
            release_id="persistent-governor-memory",
            task_id="pgm-0001",
            outcome="accepted",
            run_summary_path=Path("runs/persistent-governor-memory/review_summary.json"),
            recorded_at=datetime(2026, 5, 13, 8, 0, tzinfo=UTC),
        ),
    )
    state = store.add_epic_unresolved_finding_reference(
        "epic-1",
        UnresolvedFindingReference(
            finding_id="finding-1",
            summary="Feature review requires follow-up.",
            severity="medium",
            source_path=Path("runs/persistent-governor-memory/feature_review.json"),
        ),
    )
    state = store.add_state_review_snapshot_reference(
        "epic-1",
        StateReviewSnapshotReference(
            snapshot_path=Path("runs/persistent-governor-memory/state_review_snapshot.json"),
            captured_at=datetime(2026, 5, 13, 9, 0, tzinfo=UTC),
            release_id="persistent-governor-memory",
        ),
    )

    record = state.active_epics[0]
    assert record.epic_id == "epic-1"
    assert record.retry_count == 1
    assert record.repair_count == 2
    assert record.outcome_references[0].release_id == "persistent-governor-memory"
    assert record.unresolved_finding_references[0].finding_id == "finding-1"
    assert str(record.state_review_snapshot_references[0].snapshot_path).endswith(
        "state_review_snapshot.json"
    )


def test_durable_schema_rejects_empty_structured_ids_and_reasons() -> None:
    with pytest.raises(ValueError):
        OutcomeReference(release_id="", outcome="accepted")
    with pytest.raises(ValueError):
        UnresolvedFindingReference(finding_id="", summary="x")
    with pytest.raises(ValueError):
        UnresolvedFindingReference(finding_id="f-1", summary="")
    with pytest.raises(ValueError):
        BacklogState(candidate_epics=[{"id": "", "title": "Epic"}])
    with pytest.raises(ValueError):
        BacklogState(skipped_epics=[{"epic_id": "epic-1", "status_reason": ""}])


def test_record_recent_run_summary_prepends_deduplicates_and_trims(tmp_path: Path) -> None:
    state_path = tmp_path / "repo_state" / "demo" / "backlog_state.yaml"
    store = StateStore(state_path)

    first_time = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    second_time = datetime(2026, 5, 12, 13, 0, tzinfo=UTC)

    store.record_recent_run_summary(
        Path("runs/release-a/task-1/review_summary.json"),
        release_id="release-a",
        task_id="task-1",
        outcome="accepted",
        recorded_at=first_time,
        max_entries=2,
    )
    store.record_recent_run_summary(
        Path("runs/release-b/task-2/review_summary.json"),
        release_id="release-b",
        task_id="task-2",
        outcome="failed",
        recorded_at=second_time,
        max_entries=2,
    )
    state = store.record_recent_run_summary(
        Path("runs/release-a/task-1/review_summary.json"),
        release_id="release-a",
        task_id="task-1",
        outcome="accepted",
        recorded_at=second_time,
        max_entries=2,
    )

    assert [str(item.run_summary_path) for item in state.recent_run_summaries] == [
        "runs/release-a/task-1/review_summary.json",
        "runs/release-b/task-2/review_summary.json",
    ]
    assert state.recent_run_summaries[0].recorded_at == second_time


def test_record_recent_run_summary_requires_positive_max_entries(tmp_path: Path) -> None:
    state_path = tmp_path / "repo_state" / "demo" / "backlog_state.yaml"
    store = StateStore(state_path)

    with pytest.raises(ValueError, match="max_entries"):
        store.record_recent_run_summary(Path("runs/demo/review_summary.json"), max_entries=0)
