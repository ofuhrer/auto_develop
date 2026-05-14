from __future__ import annotations

import json
from pathlib import Path

from agentic_devloop.governor_log import (
    GovernorEventContext,
    GovernorEventType,
    build_governor_event_log_writer,
)
from agentic_devloop.paths import governor_run_paths


def test_governor_run_paths_creates_run_directory(tmp_path: Path) -> None:
    paths = governor_run_paths(runs_dir=tmp_path / "runs", run_id="run-1")

    assert paths.run_root == tmp_path / "runs" / "run-1"
    assert paths.run_root.exists()
    assert paths.log_path == paths.run_root / "governor.log"
    assert paths.raw_log_path == paths.run_root / "governor.raw.log"
    assert paths.events_path == paths.run_root / "events.jsonl"


def test_governor_writer_emits_all_log_files(tmp_path: Path) -> None:
    writer = build_governor_event_log_writer(runs_dir=tmp_path / "runs", run_id="run-1")

    writer.write(
        event_type=GovernorEventType.GOVERNOR_STARTED,
        message="Selected next epic.",
    )

    assert writer.paths.log_path.exists()
    assert writer.paths.raw_log_path.exists()
    assert writer.paths.events_path.exists()

    assert "Selected next epic." in writer.paths.log_path.read_text(encoding="utf-8")
    raw_text = writer.paths.raw_log_path.read_text(encoding="utf-8")
    assert "event=governor_started" in raw_text
    assert "message=Selected next epic." in raw_text
    assert "context=phase=governor_started artifact_count=0" in raw_text


def test_governor_events_jsonl_is_valid_and_keeps_artifact_links(tmp_path: Path) -> None:
    writer = build_governor_event_log_writer(runs_dir=tmp_path / "runs", run_id="run-1")
    artifact_one = tmp_path / "runs" / "child-release" / "release.log"
    artifact_two = tmp_path / "runs" / "child-release" / "release_summary.json"

    writer.write(
        event_type=GovernorEventType.RELEASE_COMPLETED,
        message="Child release finished.",
        artifacts=[artifact_one, artifact_two],
    )

    lines = writer.paths.events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])

    assert set(record.keys()) == {"artifacts", "context", "event_type", "message", "timestamp"}
    assert record["event_type"] == "release_completed"
    assert record["message"] == "Child release finished."
    assert record["artifacts"] == [str(artifact_one), str(artifact_two)]
    assert isinstance(record["timestamp"], str)
    assert record["context"]["phase"] == "release_completed"
    assert record["context"]["artifact_count"] == 2


def test_governor_writer_emits_typed_context_and_readable_compact_logs(tmp_path: Path) -> None:
    writer = build_governor_event_log_writer(runs_dir=tmp_path / "runs", run_id="run-1")

    writer.write(
        event_type=GovernorEventType.FINAL_VERIFICATION_COMPLETED,
        message="Final verification passed.",
        context=GovernorEventContext(
            phase=GovernorEventType.FINAL_VERIFICATION_COMPLETED,
            subphase="final_verification",
            release_id="rel-123",
            epic_id="governor-cockpit-v2",
            decision="continue",
            outcome="accepted",
            cycle_index=2,
            details={
                "tests_passed": True,
                "duration_s": 42,
                "final_review_outcome": "accepted_risk",
                "finding_adjudication_paths": ["runs/r1/a.json", "runs/r1/b.json"],
            },
        ),
    )

    raw_text = writer.paths.raw_log_path.read_text(encoding="utf-8")
    human_text = writer.paths.log_path.read_text(encoding="utf-8")
    event_line = writer.paths.events_path.read_text(encoding="utf-8").splitlines()[0]
    record = json.loads(event_line)

    assert "context=phase=final_verification_completed subphase=final_verification" in raw_text
    assert "release_id=rel-123" in raw_text
    assert "decision=continue" in raw_text
    assert "final_review_outcome:\"accepted_risk\"" in raw_text
    assert "finding_adjudication_paths:[\"runs/r1/a.json\", \"runs/r1/b.json\"]" in raw_text
    assert "(phase=final_verification_completed subphase=final_verification release_id=rel-123" in human_text
    assert record["context"] == {
        "cycle_index": 2,
        "decision": "continue",
        "details": {
            "duration_s": 42,
            "final_review_outcome": "accepted_risk",
            "finding_adjudication_paths": ["runs/r1/a.json", "runs/r1/b.json"],
            "tests_passed": True,
        },
        "epic_id": "governor-cockpit-v2",
        "outcome": "accepted",
        "phase": "final_verification_completed",
        "release_id": "rel-123",
        "subphase": "final_verification",
    }


def test_governor_writer_handles_non_json_serializable_detail_values(tmp_path: Path) -> None:
    writer = build_governor_event_log_writer(runs_dir=tmp_path / "runs", run_id="run-1")

    writer.write(
        event_type=GovernorEventType.FINAL_VERIFICATION_COMPLETED,
        message="Final verification passed.",
        context=GovernorEventContext(
            phase=GovernorEventType.FINAL_VERIFICATION_COMPLETED,
            details={"adjudication_path": Path("x")},
        ),
    )

    raw_text = writer.paths.raw_log_path.read_text(encoding="utf-8")
    event_line = writer.paths.events_path.read_text(encoding="utf-8").splitlines()[0]
    record = json.loads(event_line)

    assert "adjudication_path:\"PosixPath('x')\"" in raw_text
    assert record["context"]["details"]["adjudication_path"] == "PosixPath('x')"


def test_governor_writer_rejects_non_enum_or_mismatched_context_phase(tmp_path: Path) -> None:
    writer = build_governor_event_log_writer(runs_dir=tmp_path / "runs", run_id="run-1")

    try:
        writer.write(
            event_type=GovernorEventType.FINAL_VERIFICATION_COMPLETED,
            message="Invalid custom phase.",
            context=GovernorEventContext(phase="final_verification"),
        )
    except ValueError as error:
        assert "context.phase must be a GovernorEventType value" in str(error)
    else:
        raise AssertionError("Expected ValueError for non-enum context phase.")

    try:
        writer.write(
            event_type=GovernorEventType.FINAL_VERIFICATION_COMPLETED,
            message="Mismatched phase.",
            context=GovernorEventContext(phase=GovernorEventType.RELEASE_COMPLETED),
        )
    except ValueError as error:
        assert "context.phase must match event_type" in str(error)
    else:
        raise AssertionError("Expected ValueError for mismatched context phase.")


def test_governor_event_type_includes_required_cockpit_events() -> None:
    expected_values = {
        "state_review_completed",
        "state_refresh_summary",
        "state_refresh_error",
        "backlog_selection_completed",
        "objective_generation_completed",
        "contract_generation_completed",
        "child_release_started",
        "child_release_completed",
        "feature_review_completed",
        "repair_decision",
        "final_verification_completed",
        "finalization_decision",
        "cleanup_eligibility_evaluated",
        "next_epic_selected",
        "stop_reason_recorded",
        "governor_completed",
    }

    observed_values = {event.value for event in GovernorEventType}
    assert expected_values.issubset(observed_values)
