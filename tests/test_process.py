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
