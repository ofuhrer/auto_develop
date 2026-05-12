from __future__ import annotations

from agentic_devloop.process import run_process


def test_run_process_streams_stdout_and_stderr(tmp_path) -> None:
    streamed: list[tuple[str, str]] = []

    result = run_process(
        [
            "python3",
            "-c",
            (
                "import sys\n"
                "print('worker stdout')\n"
                "print('worker stderr', file=sys.stderr)\n"
            ),
        ],
        cwd=tmp_path,
        timeout_seconds=10,
        stream_callback=lambda stream, line: streamed.append((stream, line)),
    )

    assert result.exit_code == 0
    assert "worker stdout" in result.stdout
    assert "worker stderr" in result.stderr
    assert ("stdout", "worker stdout") in streamed
    assert ("stderr", "worker stderr") in streamed


def test_run_process_emits_streaming_heartbeats(tmp_path) -> None:
    heartbeats: list[float] = []

    result = run_process(
        [
            "python3",
            "-c",
            "import time; time.sleep(0.25); print('done')",
        ],
        cwd=tmp_path,
        timeout_seconds=10,
        stream_callback=lambda _stream, _line: None,
        heartbeat_callback=heartbeats.append,
        heartbeat_interval_seconds=0.05,
    )

    assert result.exit_code == 0
    assert heartbeats
    assert all(value > 0 for value in heartbeats)
