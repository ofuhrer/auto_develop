from __future__ import annotations

import yaml

from agentic_devloop.status import load_run_summaries


def test_load_run_summaries_reads_decision_files(tmp_path) -> None:
    decision_path = tmp_path / "runs" / "run-1" / "task-1" / "evidence" / "decision.yaml"
    decision_path.parent.mkdir(parents=True)
    decision_path.write_text(
        yaml.safe_dump({"task_id": "task-1", "decision": "accepted"}),
        encoding="utf-8",
    )

    summaries = load_run_summaries(tmp_path / "runs")

    assert len(summaries) == 1
    assert summaries[0].run_id == "run-1"
    assert summaries[0].task_id == "task-1"
    assert summaries[0].decision == "accepted"


def test_load_run_summaries_falls_back_to_persisted_run_state(tmp_path) -> None:
    state_path = tmp_path / "runs" / "run-2" / "task-2" / "run_state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        '{"task_id":"task-2","state":"INTERRUPTED"}\n',
        encoding="utf-8",
    )

    summaries = load_run_summaries(tmp_path / "runs")

    assert len(summaries) == 1
    assert summaries[0].run_id == "run-2"
    assert summaries[0].task_id == "task-2"
    assert summaries[0].decision == "interrupted"
