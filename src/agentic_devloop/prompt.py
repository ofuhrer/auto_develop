from __future__ import annotations

from pathlib import Path

import yaml

from agentic_devloop.models import ContextBundle, TaskContract
from agentic_devloop.security import redact_text


def build_executor_prompt(task: TaskContract, context: ContextBundle | None = None) -> str:
    contract_yaml = yaml.safe_dump(task.model_dump(mode="json"), sort_keys=False)
    context_text = _context_text(context)
    return redact_text(f"""# Bounded Development Task

You are executing one bounded task inside an isolated Git worktree.

## Operating Rules

- Complete the task autonomously within the contract.
- Do not expand scope.
- Do not edit files outside `allowed_files`.
- Do not make any forbidden change.
- Do not weaken verification, assertions, fixtures, tolerances, or scientific constraints.
- Do not change fixture, benchmark, or tolerance semantics unless the task contract explicitly allows it.
- If remote dispatch is declared, do not fake remote execution; report missing remote execution honestly.
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
{context_text}
""")


def write_executor_prompt(
    task: TaskContract,
    path: Path,
    context: ContextBundle | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_executor_prompt(task, context), encoding="utf-8")
    return path


def _context_text(context: ContextBundle | None) -> str:
    if context is None or not context.sections:
        return ""

    sections = ["\n## External Repo Context\n"]
    for section in context.sections:
        sections.append(f"### {section.name}\n")
        sections.append(f"Source: `{section.source_path}`\n\n")
        sections.append("```text\n")
        sections.append(section.content.rstrip())
        sections.append("\n```\n")
    return "\n".join(sections)
