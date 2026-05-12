from __future__ import annotations

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
    assert (tmp_path / "verification" / "verification.log").exists()


def test_verification_runner_stops_on_failure(tmp_path) -> None:
    runner = VerificationRunner(timeout_seconds=5)

    results = runner.run(
        commands=["exit 7", "printf skipped"],
        worktree_path=tmp_path,
        output_dir=tmp_path / "verification",
    )

    assert len(results) == 1
    assert results[0].exit_code == 7
