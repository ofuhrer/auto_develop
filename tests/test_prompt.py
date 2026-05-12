from __future__ import annotations

from pathlib import Path

from agentic_devloop.models import TaskContract
from agentic_devloop.models import ContextBundle, ContextSection
from agentic_devloop.prompt import build_executor_prompt
from agentic_devloop.yaml_io import load_yaml_model


ROOT = Path(__file__).resolve().parents[1]


def test_executor_prompt_contains_contract_and_autonomy_rules() -> None:
    task = load_yaml_model(ROOT / "contracts" / "rr-0001.yaml", TaskContract)

    prompt = build_executor_prompt(task)

    assert "Complete the task autonomously within the contract." in prompt
    assert "task_id: rr-0001" in prompt
    assert "allowed_files:" in prompt


def test_executor_prompt_includes_external_context() -> None:
    task = load_yaml_model(ROOT / "contracts" / "rr-0001.yaml", TaskContract)
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
