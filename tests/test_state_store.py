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


def test_load_legacy_skipped_epics_string_list_shape(tmp_path: Path) -> None:
    state_path = tmp_path / "repo_state" / "demo" / "backlog_state.yaml"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        """
skipped_epics:
  - epic-1
  - epic-2
""".strip()
        + "\n",
        encoding="utf-8",
    )

    state = StateStore(state_path).load()

    assert [record.epic_id for record in state.skipped_epics] == ["epic-1", "epic-2"]


def test_load_legacy_and_typed_epic_record_entries_together(tmp_path: Path) -> None:
    state_path = tmp_path / "repo_state" / "demo" / "backlog_state.yaml"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        """
completed_epic_records:
  - epic-1
  - epic_id: epic-2
    status_reason: reviewed
""".strip()
        + "\n",
        encoding="utf-8",
    )

    state = StateStore(state_path).load()

    assert [record.epic_id for record in state.completed_epic_records] == ["epic-1", "epic-2"]
    assert state.completed_epic_records[1].status_reason == "reviewed"


def test_mixed_legacy_and_typed_records_remain_consistent_across_lifecycle(tmp_path: Path) -> None:
    state_path = tmp_path / "repo_state" / "demo" / "backlog_state.yaml"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        """
active_epic: epic-target
active_epics:
  - epic-target
completed_epics:
  - epic-target
blocked_epics:
  - epic-target
completed_epic_records:
  - epic_id: epic-target
    status_reason: prior-completion
blocked_epic_records:
  - epic-target
skipped_epics:
  - epic-target
reviewed_epics:
  - epic_id: epic-target
    status_reason: prior-review
""".strip()
        + "\n",
        encoding="utf-8",
    )

    store = StateStore(state_path)

    active = store.mark_active_epic("epic-target")
    assert active.active_epic == "epic-target"
    assert active.completed_epics == []
    assert active.blocked_epics == []
    assert [record.epic_id for record in active.active_epics] == ["epic-target"]
    assert active.completed_epic_records == []
    assert active.blocked_epic_records == []
    assert active.skipped_epics == []
    assert [record.epic_id for record in active.reviewed_epics] == ["epic-target"]
    assert active.reviewed_epics[0].status_reason == "prior-review"

    completed = store.mark_completed_epic("epic-target")
    assert completed.active_epic is None
    assert completed.completed_epics == ["epic-target"]
    assert completed.blocked_epics == []
    assert completed.active_epics == []
    assert [record.epic_id for record in completed.completed_epic_records] == ["epic-target"]
    assert completed.blocked_epic_records == []
    assert completed.skipped_epics == []
    assert [record.epic_id for record in completed.reviewed_epics] == ["epic-target"]
    assert completed.reviewed_epics[0].status_reason == "prior-review"

    blocked = store.mark_blocked_epic("epic-target", blocked_reason="needs-dependency")
    assert blocked.active_epic is None
    assert blocked.completed_epics == []
    assert blocked.blocked_epics == ["epic-target"]
    assert blocked.active_epics == []
    assert blocked.completed_epic_records == []
    assert [record.epic_id for record in blocked.blocked_epic_records] == ["epic-target"]
    assert blocked.blocked_epic_records[0].blocked_reason == "needs-dependency"
    assert blocked.skipped_epics == []
    assert blocked.reviewed_epics == []

    skipped = store.mark_skipped_epic("epic-target", status_reason="deferred")
    assert skipped.active_epic is None
    assert skipped.completed_epics == []
    assert skipped.blocked_epics == []
    assert skipped.active_epics == []
    assert skipped.completed_epic_records == []
    assert skipped.blocked_epic_records == []
    assert [record.epic_id for record in skipped.skipped_epics] == ["epic-target"]
    assert skipped.skipped_epics[0].status_reason == "deferred"
    assert skipped.reviewed_epics == []

    reviewed = store.mark_reviewed_epic("epic-target", status_reason="manual-review-complete")
    assert reviewed.active_epic is None
    assert reviewed.completed_epics == []
    assert reviewed.blocked_epics == []
    assert reviewed.active_epics == []
    assert reviewed.completed_epic_records == []
    assert reviewed.blocked_epic_records == []
    assert reviewed.skipped_epics == []
    assert [record.epic_id for record in reviewed.reviewed_epics] == ["epic-target"]
    assert reviewed.reviewed_epics[0].status_reason == "manual-review-complete"


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
            severity="moderate",
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


@pytest.mark.parametrize(
    ("mark_method", "record_list"),
    [
        ("mark_completed_epic", "completed_epic_records"),
        ("mark_blocked_epic", "blocked_epic_records"),
        ("mark_skipped_epic", "skipped_epics"),
    ],
)
def test_add_epic_references_reuses_non_active_record(
    tmp_path: Path,
    mark_method: str,
    record_list: str,
) -> None:
    state_path = tmp_path / "repo_state" / "demo" / "backlog_state.yaml"
    store = StateStore(state_path)
    epic_id = "epic-1"

    if mark_method == "mark_skipped_epic":
        getattr(store, mark_method)(epic_id, status_reason="not-selected")
    else:
        getattr(store, mark_method)(epic_id)

    outcome = OutcomeReference(
        release_id="persistent-governor-memory",
        task_id="pgm-0001",
        outcome="accepted",
        run_summary_path=Path("runs/persistent-governor-memory/review_summary.json"),
        recorded_at=datetime(2026, 5, 13, 8, 0, tzinfo=UTC),
    )
    state = store.add_epic_outcome_reference(epic_id, outcome)

    records = getattr(state, record_list)
    assert len(records) == 1
    assert records[0].epic_id == epic_id
    assert len(records[0].outcome_references) == 1
    assert records[0].outcome_references[0].release_id == "persistent-governor-memory"
    assert state.active_epics == []


def test_add_epic_reference_uses_legacy_completed_epics_bucket(tmp_path: Path) -> None:
    state_path = tmp_path / "repo_state" / "demo" / "backlog_state.yaml"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        """
completed_epics:
  - epic-1
""".strip()
        + "\n",
        encoding="utf-8",
    )
    store = StateStore(state_path)

    state = store.add_epic_outcome_reference(
        "epic-1",
        OutcomeReference(
            release_id="persistent-governor-memory",
            task_id="pgm-0002",
            outcome="accepted",
            run_summary_path=Path("runs/persistent-governor-memory/review_summary.json"),
            recorded_at=datetime(2026, 5, 13, 8, 0, tzinfo=UTC),
        ),
    )

    assert state.active_epics == []
    assert [record.epic_id for record in state.completed_epic_records] == ["epic-1"]
    assert state.completed_epic_records[0].outcome_references[0].release_id == "persistent-governor-memory"


def test_epic_transition_preserves_existing_memory_record_fields(tmp_path: Path) -> None:
    state_path = tmp_path / "repo_state" / "demo" / "backlog_state.yaml"
    store = StateStore(state_path)
    epic_id = "epic-1"

    store.increment_epic_retry_count(epic_id, amount=2)
    store.increment_epic_repair_count(epic_id, amount=3)
    store.add_epic_outcome_reference(
        epic_id,
        OutcomeReference(
            release_id="persistent-governor-memory",
            task_id="pgm-0002",
            outcome="accepted",
            run_summary_path=Path("runs/persistent-governor-memory/review_summary.json"),
            recorded_at=datetime(2026, 5, 13, 8, 0, tzinfo=UTC),
        ),
    )

    store.mark_completed_epic(epic_id)
    blocked = store.mark_blocked_epic(epic_id, blocked_reason="awaiting-dependent-epic")
    blocked_record = blocked.blocked_epic_records[0]

    assert blocked_record.epic_id == epic_id
    assert blocked_record.retry_count == 2
    assert blocked_record.repair_count == 3
    assert len(blocked_record.outcome_references) == 1
    assert blocked_record.outcome_references[0].task_id == "pgm-0002"
    assert blocked_record.blocked_reason == "awaiting-dependent-epic"


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


def test_durable_schema_rejects_invalid_outcome_and_severity_values() -> None:
    with pytest.raises(ValueError):
        OutcomeReference(release_id="release-1", outcome="unknown")
    with pytest.raises(ValueError):
        UnresolvedFindingReference(finding_id="f-1", summary="x", severity="medium")


def test_durable_schema_rejects_extra_epic_memory_fields(tmp_path: Path) -> None:
    state_path = tmp_path / "repo_state" / "demo" / "backlog_state.yaml"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        """
active_epics:
  - epic_id: epic-1
    unknown_field: should-fail
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        StateStore(state_path).load()


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
