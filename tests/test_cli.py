from __future__ import annotations

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
