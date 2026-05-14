from __future__ import annotations

import sys

from agentic_devloop.verification import VerificationRunner


def test_verification_runner_records_output(tmp_path) -> None:
    runner = VerificationRunner(timeout_seconds=5)

    results = runner.run(
        commands=["printf ok"],
        worktree_path=tmp_path,
        output_dir=tmp_path / "verification",
    )

    assert len(results) == 1
    assert results[0].exit_code == 0
    assert results[0].stdout_path is not None
    assert results[0].stdout_path.read_text(encoding="utf-8") == "ok"
    log = (tmp_path / "verification" / "verification.log").read_text(encoding="utf-8")
    assert "original_command=printf ok" in log
    assert "resolved_command=printf ok" in log
    assert f"cwd={tmp_path}" in log
    assert "timeout_seconds=5" in log
    assert "env_additions=<none>" in log
    assert "stdout_path=" in log
    assert "stderr_path=" in log
    assert "exit_code=0" in log
    assert "failure_reason=<none>" in log
    assert "stdout_excerpt:\nok" in log
    assert "stderr_excerpt:\n<empty>" in log


def test_verification_runner_stops_on_failure(tmp_path) -> None:
    runner = VerificationRunner(timeout_seconds=5)

    results = runner.run(
        commands=["exit 7", "printf skipped"],
        worktree_path=tmp_path,
        output_dir=tmp_path / "verification",
    )

    assert len(results) == 1
    assert results[0].exit_code == 7


def test_verification_runner_records_failure_excerpts(tmp_path) -> None:
    runner = VerificationRunner(timeout_seconds=5)

    results = runner.run(
        commands=["printf failure-out; printf failure-err >&2; exit 3"],
        worktree_path=tmp_path,
        output_dir=tmp_path / "verification",
    )

    log = (tmp_path / "verification" / "verification.log").read_text(encoding="utf-8")
    assert results[0].exit_code == 3
    assert "exit_code=3" in log
    assert "failure_reason=nonzero_exit_3" in log
    assert "stdout_excerpt:\nfailure-out" in log
    assert "stderr_excerpt:\nfailure-err" in log


def test_verification_runner_without_shared_runtime_keeps_local_venv_command_and_fails(tmp_path) -> None:
    runner = VerificationRunner(timeout_seconds=5)

    results = runner.run(
        commands=['.venv/bin/python -c "print(123)"'],
        worktree_path=tmp_path,
        output_dir=tmp_path / "verification",
    )

    assert len(results) == 1
    assert results[0].exit_code != 0
    assert results[0].command == '.venv/bin/python -c "print(123)"'
    log = (tmp_path / "verification" / "verification.log").read_text(encoding="utf-8")
    assert "original_command=.venv/bin/python -c \"print(123)\"" in log
    assert "resolved_command=.venv/bin/python -c \"print(123)\"" in log
    assert "failure_reason=nonzero_exit_" in log


def test_verification_runner_uses_runtime_python_and_env(tmp_path) -> None:
    runner = VerificationRunner(timeout_seconds=5)

    results = runner.run(
        commands=['.venv/bin/python -c "import os; print(os.environ[\'SHARED_RT\'])"'],
        worktree_path=tmp_path,
        output_dir=tmp_path / "verification",
        runtime_python_path=sys.executable,
        runtime_env={"SHARED_RT": "enabled"},
    )

    assert len(results) == 1
    assert results[0].exit_code == 0
    assert results[0].command.startswith(sys.executable)
    assert results[0].stdout_path.read_text(encoding="utf-8").strip() == "enabled"
    log = (tmp_path / "verification" / "verification.log").read_text(encoding="utf-8")
    assert "original_command=.venv/bin/python -c " in log
    assert f"resolved_command={sys.executable}" in log
    assert "env_additions=SHARED_RT=enabled" in log
