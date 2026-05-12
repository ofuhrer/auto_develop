from __future__ import annotations

import pytest

from agentic_devloop.cli import main


def test_cli_help_exits_successfully(capsys) -> None:
    exit_code = main([])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "agent-loop" in captured.out


def test_init_prints_project_and_repo(capsys) -> None:
    exit_code = main(["init", "--project", "rust_rockfall", "--repo", "/tmp/rust_rockfall"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "project=rust_rockfall" in captured.out
    assert "repo=/tmp/rust_rockfall" in captured.out


def test_config_prints_project_config(capsys) -> None:
    exit_code = main(["config", "--project", "rust_rockfall"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert '"project_id": "rust_rockfall"' in captured.out


def test_run_task_command_is_registered(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["run-task", "--help"])

    captured = capsys.readouterr()

    assert error.value.code == 0
    assert "--contract" in captured.out
    assert "--push-on-accept" in captured.out


def test_run_release_command_is_registered(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["run-release", "--help"])

    captured = capsys.readouterr()

    assert error.value.code == 0
    assert "--release" in captured.out
    assert "--continue-on-failure" in captured.out
