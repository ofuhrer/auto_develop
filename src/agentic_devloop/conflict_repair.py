from __future__ import annotations

import json
from pathlib import Path

from agentic_devloop.models import TaskContract


def conflicted_files(repo_path: Path) -> list[str]:
    import subprocess

    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def write_conflict_repair_prompt(
    *,
    path: Path,
    task: TaskContract,
    conflicted: list[str],
    failure: str,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    prompt = "\n".join(
        [
            "# Bounded Conflict Repair Task",
            "",
            "Resolve only the listed Git conflict files. Do not expand scope.",
            "Preserve the task contract and latest base branch behavior.",
            "After resolving conflicts, stage the resolved files with `git add`.",
            "",
            "## Conflicted Files",
            "",
            *[f"- {file}" for file in conflicted],
            "",
            "## Original Task Contract",
            "",
            "```json",
            json.dumps(task.model_dump(mode="json"), indent=2),
            "```",
            "",
            "## Finalization Failure",
            "",
            "```text",
            failure,
            "```",
            "",
        ]
    )
    path.write_text(prompt, encoding="utf-8")
    return path
