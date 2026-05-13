from __future__ import annotations

from pathlib import Path

from agentic_devloop.models import ContextBundle, ContextSection, TaskContract
from agentic_devloop.prompt import build_executor_prompt


def test_executor_prompt_contains_contract_and_autonomy_rules() -> None:
    task = _task_contract()

    prompt = build_executor_prompt(task)

    assert "Complete the task autonomously within the contract." in prompt
    assert "task_id: rr-0001" in prompt
    assert "allowed_files:" in prompt


def test_executor_prompt_includes_external_context() -> None:
    task = _task_contract()
    context = ContextBundle(
        sections=[
            ContextSection(
                name="architecture_summary",
                source_path=Path("repo_state/demo/architecture_summary.md"),
                content="Use bounded worktrees.",
            )
        ]
    )

    prompt = build_executor_prompt(task, context)

    assert "## External Repo Context" in prompt
    assert "Use bounded worktrees." in prompt


def _task_contract() -> TaskContract:
    return TaskContract(
        task_id="rr-0001",
        release_id="v0.8.0",
        title="Add regression test",
        task_type="code_only",
        budget_class="S",
        objective="Add one bounded regression test.",
        allowed_files=["tests/**"],
        forbidden_changes=["Do not weaken assertions."],
        required_evidence=["git diff", "changed-files list"],
        verification={"commands": ["true"]},
        stop_conditions=["Stop if scope changes."],
    )
