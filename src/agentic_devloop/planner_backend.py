from __future__ import annotations

import shlex
from pathlib import Path

from agentic_devloop.models import ExecutorConfig, ReleaseObjective, TaskContract
from agentic_devloop.process import run_process


class PlannerBackendError(RuntimeError):
    pass


class CodexPlannerBackend:
    def __init__(self, *, config: ExecutorConfig, repo_path: Path, output_dir: Path) -> None:
        self.config = config
        self.repo_path = repo_path
        self.output_dir = output_dir

    def generate(
        self,
        *,
        prompt: str,
        objective: ReleaseObjective,
        existing_contracts: list[TaskContract],
        model: str,
    ) -> str:
        del objective, existing_contracts
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = self.output_dir / "planner_stdout.log"
        stderr_path = self.output_dir / "planner_stderr.log"
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
        if result.exit_code != 0:
            quoted = " ".join(shlex.quote(part) for part in command)
            message = result.stderr.strip() or result.stdout.strip()
            raise PlannerBackendError(f"planner command failed ({quoted}): {message}")
        return result.stdout
