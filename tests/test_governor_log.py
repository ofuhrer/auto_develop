from __future__ import annotations

import json
from pathlib import Path

from agentic_devloop.governor_log import GovernorEventType, build_governor_event_log_writer
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

    assert record["event_type"] == "release_completed"
    assert record["message"] == "Child release finished."
    assert record["artifacts"] == [str(artifact_one), str(artifact_two)]
    assert isinstance(record["timestamp"], str)
