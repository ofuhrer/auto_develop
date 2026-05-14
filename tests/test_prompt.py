from __future__ import annotations

from pathlib import Path

from agentic_devloop.models import (
    ContextBundle,
    ContextBundleManifest,
    ContextPhase,
    ContextSection,
    ContextTruncationRecord,
    TaskContract,
)
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
    assert "## Worker Context Bundle Summary" in prompt
    assert "worker_context_manifest.json" in prompt


def test_executor_prompt_includes_bundle_truncation_boundaries() -> None:
    task = _task_contract()
    context = ContextBundle(
        sections=[
            ContextSection(
                name="architecture_summary",
                source_path=Path("repo_state/demo/architecture_summary.md"),
                content="A" * 8,
            )
        ],
        manifest=ContextBundleManifest(
            phase=ContextPhase.WORKER,
            included_categories=["architecture_summary"],
            omitted_categories=["backlog_state"],
            chars_by_category={"architecture_summary": 8},
            total_chars=8,
            truncation_records=[
                ContextTruncationRecord(
                    category="backlog_state",
                    source_path=Path("repo_state/demo/backlog_state.yaml"),
                    original_chars=20,
                    included_chars=0,
                    omitted_chars=20,
                    reason="omitted_after_budget",
                )
            ],
        ),
    )

    prompt = build_executor_prompt(task, context)

    assert "### Truncation Boundaries" in prompt
    assert "backlog_state: included=0 omitted=20 reason=omitted_after_budget" in prompt


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
