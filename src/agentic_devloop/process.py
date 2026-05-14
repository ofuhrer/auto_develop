from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class ProcessOutput:
    command: list[str] | str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False


def run_process(
    command: list[str] | str,
    *,
    cwd: Path,
    timeout_seconds: int,
    shell: bool = False,
    input_text: str | None = None,
    stream_callback: Callable[[str, str], None] | None = None,
    heartbeat_callback: Callable[[float], None] | None = None,
    heartbeat_interval_seconds: float = 120.0,
    env_additions: dict[str, str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ProcessOutput:
    started_at = clock()
    env = os.environ.copy()
    if env_additions:
        env.update(env_additions)
    if stream_callback is not None:
        return _run_process_streamed(
            command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            shell=shell,
            input_text=input_text,
            stream_callback=stream_callback,
            heartbeat_callback=heartbeat_callback,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            env=env,
            started_at=started_at,
            clock=clock,
        )
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            timeout=timeout_seconds,
            text=True,
            capture_output=True,
            shell=shell,
            input=input_text,
            env=env,
            check=False,
        )
        return ProcessOutput(
            command=command,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=clock() - started_at,
        )
    except subprocess.TimeoutExpired as error:
        return ProcessOutput(
            command=command,
            exit_code=124,
            stdout=error.stdout or "",
            stderr=error.stderr or "",
            duration_seconds=clock() - started_at,
            timed_out=True,
        )


def _run_process_streamed(
    command: list[str] | str,
    *,
    cwd: Path,
    timeout_seconds: int,
    shell: bool,
    input_text: str | None,
    stream_callback: Callable[[str, str], None],
    heartbeat_callback: Callable[[float], None] | None,
    heartbeat_interval_seconds: float,
    env: dict[str, str],
    started_at: float,
    clock: Callable[[], float],
) -> ProcessOutput:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        text=True,
        shell=shell,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def read_stream(name: str, lines: list[str]) -> None:
        stream = process.stdout if name == "stdout" else process.stderr
        assert stream is not None
        for line in iter(stream.readline, ""):
            lines.append(line)
            stream_callback(name, line.rstrip("\n"))
        stream.close()

    stdout_thread = threading.Thread(target=read_stream, args=("stdout", stdout_lines), daemon=True)
    stderr_thread = threading.Thread(target=read_stream, args=("stderr", stderr_lines), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    if input_text is not None:
        assert process.stdin is not None
        try:
            process.stdin.write(input_text)
            process.stdin.close()
        except BrokenPipeError:
            pass

    timed_out = False
    next_heartbeat = started_at + heartbeat_interval_seconds
    deadline = started_at + timeout_seconds
    try:
        while True:
            exit_code = process.poll()
            if exit_code is not None:
                break
            now = clock()
            if now >= deadline:
                timed_out = True
                process.kill()
                exit_code = 124
                process.wait()
                break
            if heartbeat_callback is not None and heartbeat_interval_seconds > 0 and now >= next_heartbeat:
                heartbeat_callback(now - started_at)
                while next_heartbeat <= now:
                    next_heartbeat += heartbeat_interval_seconds
            time.sleep(min(0.2, max(0.01, deadline - now)))
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        exit_code = 124
        process.wait()

    stdout_thread.join(timeout=1)
    stderr_thread.join(timeout=1)
    return ProcessOutput(
        command=command,
        exit_code=exit_code,
        stdout="".join(stdout_lines),
        stderr="".join(stderr_lines),
        duration_seconds=clock() - started_at,
        timed_out=timed_out,
    )
