from __future__ import annotations

import shlex
from pathlib import Path
from typing import Callable

from agentic_devloop.models import ExecutorConfig, ExecutorResult
from agentic_devloop.process import run_process


class CodexExecutor:
    def __init__(
        self,
        config: ExecutorConfig,
        *,
        stream_callback: Callable[[str, str], None] | None = None,
        heartbeat_callback: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self.stream_callback = stream_callback
        self.heartbeat_callback = heartbeat_callback

    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = output_dir / "executor_stdout.log"
        stderr_path = output_dir / "executor_stderr.log"
        command = [
            "codex",
            "exec",
            "--model",
            self.config.model,
            "--sandbox",
            "workspace-write",
            "-",
        ]
        prompt_text = prompt_path.read_text(encoding="utf-8")
        result = run_process(
            command,
            cwd=worktree_path,
            timeout_seconds=self.config.max_walltime_minutes * 60,
            input_text=prompt_text,
            stream_callback=self.stream_callback,
            heartbeat_callback=self.heartbeat_callback,
        )

        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")

        return ExecutorResult(
            command=[shlex.quote(part) for part in command],
            exit_code=result.exit_code,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            duration_seconds=result.duration_seconds,
            timed_out=result.timed_out,
            backend=self.config.type,
            model=self.config.model,
            prompt_chars=len(prompt_text),
            stdout_chars=len(result.stdout),
            stderr_chars=len(result.stderr),
        )
