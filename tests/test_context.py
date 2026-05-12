from __future__ import annotations

from pathlib import Path

import pytest

from agentic_devloop.context import ContextBudgetError, enforce_context_budget, load_context_bundle
from agentic_devloop.models import Budget, ExecutorConfig, ProjectConfig, TaskContract, VerificationProfile


def test_load_context_bundle_reads_repo_state_and_filters_known_failures(tmp_path) -> None:
    repo = tmp_path / "repo"
    state = repo / "repo_state" / "demo"
    state.mkdir(parents=True)
    (state / "architecture_summary.md").write_text("Architecture summary.\n", encoding="utf-8")
    (state / "active_constraints.yaml").write_text("constraints: []\n", encoding="utf-8")
    (state / "backlog_state.yaml").write_text("active_goal: autonomous governor\n", encoding="utf-8")
    (state / "known_failures.md").write_text("unrelated failure\n", encoding="utf-8")

    config = _project_config(repo)
    task = _task_contract("demo-0001")

    context = load_context_bundle(config, task)

    assert [section.name for section in context.sections] == [
        "architecture_summary",
        "active_constraints",
        "backlog_state",
    ]


def test_load_context_bundle_includes_relevant_known_failures(tmp_path) -> None:
    repo = tmp_path / "repo"
    state = repo / "repo_state" / "demo"
    state.mkdir(parents=True)
    (state / "known_failures.md").write_text("demo-0001 failed before\n", encoding="utf-8")

    context = load_context_bundle(_project_config(repo), _task_contract("demo-0001"))

    assert [section.name for section in context.sections] == ["known_failures"]


def test_context_budget_is_enforced() -> None:
    from agentic_devloop.models import ContextBundle, ContextSection

    context = ContextBundle(
        sections=[
            ContextSection(name="big", source_path=Path("big.md"), content="x" * 20),
        ]
    )

    with pytest.raises(ContextBudgetError, match="exceeding budget"):
        enforce_context_budget(context, max_chars=10)


def _project_config(repo: Path) -> ProjectConfig:
    return ProjectConfig(
        project_id="demo",
        repo_path=repo,
        default_base_branch="main",
        worktree_root=repo / "worktrees",
        repo_state_path=Path("repo_state/demo"),
        executor=ExecutorConfig(
            type="codex_cli",
            model="gpt-5.4-mini",
            max_walltime_minutes=5,
        ),
        verification_profiles={"default": VerificationProfile(commands=["pytest"])},
        budget=Budget(
            max_executor_attempts_per_task=2,
            max_strong_model_calls_per_release=10,
            max_changed_files_per_task=8,
            max_diff_lines_per_task=600,
        ),
    )


def _task_contract(task_id: str) -> TaskContract:
    return TaskContract(
        task_id=task_id,
        release_id="demo-release",
        title="Demo task",
        budget_class="S",
        objective="Do a demo task.",
        allowed_files=["README.md"],
        required_evidence=["git diff"],
        verification={"commands": ["pytest"]},
        stop_conditions=["Verification fails twice."],
    )
