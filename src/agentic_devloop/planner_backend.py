from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_devloop.models import ExecutorConfig, ReleaseObjective, TaskContract
from agentic_devloop.process import run_process


class PlannerBackendError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlannerBackendResult:
    raw_output: str | dict[str, Any]
    stdout_path: Path
    stderr_path: Path
    metadata_path: Path


class CodexPlannerBackend:
    def __init__(self, *, config: ExecutorConfig, repo_path: Path, output_dir: Path | None = None) -> None:
        self.config = config
        self.repo_path = repo_path
        self.output_dir = output_dir or Path("runs") / "planner_backend"

    def with_output_dir(self, output_dir: Path) -> "CodexPlannerBackend":
        return CodexPlannerBackend(config=self.config, repo_path=self.repo_path, output_dir=output_dir)

    def generate(
        self,
        *,
        prompt: str,
        objective: ReleaseObjective,
        existing_contracts: list[TaskContract],
        model: str,
    ) -> PlannerBackendResult:
        del objective, existing_contracts
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = self.output_dir / "planner_stdout.log"
        stderr_path = self.output_dir / "planner_stderr.log"
        metadata_path = self.output_dir / "planner_metadata.json"
        command = [
            "codex",
            "exec",
            "--model",
            model,
            "--sandbox",
            "workspace-write",
            "-",
        ]
        result = run_process(
            command,
            cwd=self.repo_path,
            timeout_seconds=self.config.max_walltime_minutes * 60,
            input_text=prompt,
        )
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        metadata_path.write_text(
            json.dumps(
                {
                    "backend": self.config.type,
                    "model": model,
                    "command": [shlex.quote(part) for part in command],
                    "exit_code": result.exit_code,
                    "duration_seconds": result.duration_seconds,
                    "timed_out": result.timed_out,
                    "prompt_chars": len(prompt),
                    "stdout_chars": len(result.stdout),
                    "stderr_chars": len(result.stderr),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if result.exit_code != 0:
            quoted = " ".join(shlex.quote(part) for part in command)
            message = result.stderr.strip() or result.stdout.strip()
            raise PlannerBackendError(f"planner command failed ({quoted}): {message}")
        return PlannerBackendResult(
            raw_output=result.stdout,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            metadata_path=metadata_path,
        )
