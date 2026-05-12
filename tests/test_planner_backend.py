from __future__ import annotations

import json

from agentic_devloop.models import ExecutorConfig, ReleaseObjective
from agentic_devloop.planner_backend import CodexPlannerBackend
from agentic_devloop.process import ProcessOutput


def test_codex_planner_backend_writes_stdout_stderr_and_metadata(monkeypatch, tmp_path) -> None:
    seen = {}

    def fake_run_process(command, *, cwd, timeout_seconds, input_text, shell=False):
        seen["command"] = command
        seen["cwd"] = cwd
        seen["timeout_seconds"] = timeout_seconds
        seen["input_text"] = input_text
        seen["shell"] = shell
        return ProcessOutput(
            command=command,
            exit_code=0,
            stdout='{"release_id": "v1", "generated_contracts": []}\n',
            stderr="",
            duration_seconds=1.5,
        )

    monkeypatch.setattr("agentic_devloop.planner_backend.run_process", fake_run_process)

    backend = CodexPlannerBackend(
        config=ExecutorConfig(type="codex_cli", model="planner", max_walltime_minutes=3),
        repo_path=tmp_path,
        output_dir=tmp_path / "planner",
    )
    result = backend.generate(
        prompt="plan this",
        objective=ReleaseObjective(
            release_id="v1",
            title="Release",
            objective="Do a thing.",
            acceptance_criteria=["Thing is done."],
        ),
        existing_contracts=[],
        model="planner",
    )

    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))

    assert result.raw_output.startswith('{"release_id"')
    assert result.stdout_path.read_text(encoding="utf-8") == result.raw_output
    assert result.stderr_path.read_text(encoding="utf-8") == ""
    assert metadata["model"] == "planner"
    assert metadata["exit_code"] == 0
    assert metadata["prompt_chars"] == len("plan this")
    assert seen["timeout_seconds"] == 180


def test_codex_planner_backend_can_be_retargeted_to_plan_directory(tmp_path) -> None:
    backend = CodexPlannerBackend(
        config=ExecutorConfig(type="codex_cli", model="planner", max_walltime_minutes=3),
        repo_path=tmp_path,
        output_dir=tmp_path / "old",
    )

    retargeted = backend.with_output_dir(tmp_path / "runs" / "plan" / "planner_backend")

    assert retargeted.output_dir == tmp_path / "runs" / "plan" / "planner_backend"
    assert backend.output_dir == tmp_path / "old"
