from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


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
) -> ProcessOutput:
    started_at = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            timeout=timeout_seconds,
            text=True,
            capture_output=True,
            shell=shell,
            input=input_text,
            check=False,
        )
        return ProcessOutput(
            command=command,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - started_at,
        )
    except subprocess.TimeoutExpired as error:
        return ProcessOutput(
            command=command,
            exit_code=124,
            stdout=error.stdout or "",
            stderr=error.stderr or "",
            duration_seconds=time.monotonic() - started_at,
            timed_out=True,
        )
