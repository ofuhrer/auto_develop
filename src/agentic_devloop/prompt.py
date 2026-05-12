from __future__ import annotations

from pathlib import Path

import yaml

from agentic_devloop.models import TaskContract


def build_executor_prompt(task: TaskContract) -> str:
    contract_yaml = yaml.safe_dump(task.model_dump(mode="json"), sort_keys=False)
    return f"""# Bounded Development Task

You are executing one bounded task inside an isolated Git worktree.

## Operating Rules

- Complete the task autonomously within the contract.
- Do not expand scope.
- Do not edit files outside `allowed_files`.
- Do not make any forbidden change.
- Do not weaken verification, assertions, fixtures, tolerances, or scientific constraints.
- Run only commands needed for implementation and verification.
- Stop only if the task cannot be completed without violating the contract.

## Required Output

Before finishing, provide a concise summary with:

- Files changed.
- Verification commands run.
- Verification result.
- Any risks or follow-up work.

## Task Contract

```yaml
{contract_yaml}```
"""


def write_executor_prompt(task: TaskContract, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_executor_prompt(task), encoding="utf-8")
    return path
