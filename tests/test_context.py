from __future__ import annotations

from pathlib import Path

import pytest

from agentic_devloop.context import (
    ContextBudgetError,
    build_phase_context_bundle,
    enforce_context_budget,
    load_context_bundle,
)
from agentic_devloop.models import (
    Budget,
    ContextPhase,
    ExecutorConfig,
    ProjectConfig,
    TaskContract,
    VerificationProfile,
)


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
    assert context.manifest is not None
    assert context.manifest.phase == ContextPhase.WORKER
    assert context.manifest.included_categories == [
        "architecture_summary",
        "active_constraints",
        "backlog_state",
    ]
    assert context.manifest.omitted_categories == ["known_failures"]
    assert context.manifest.total_chars == context.total_chars


def test_load_context_bundle_includes_relevant_known_failures(tmp_path) -> None:
    repo = tmp_path / "repo"
    state = repo / "repo_state" / "demo"
    state.mkdir(parents=True)
    (state / "known_failures.md").write_text("demo-0001 failed before\n", encoding="utf-8")

    context = load_context_bundle(_project_config(repo), _task_contract("demo-0001"))

    assert [section.name for section in context.sections] == ["known_failures"]


def test_phase_bundles_record_categories_and_sizes(tmp_path) -> None:
    repo = tmp_path / "repo"
    state = repo / "repo_state" / "demo"
    state.mkdir(parents=True)
    (state / "active_constraints.yaml").write_text("constraints: [a]\n", encoding="utf-8")
    (state / "release_plan.yaml").write_text("release_id: demo\n", encoding="utf-8")
    (state / "backlog_state.yaml").write_text("active_goal: demo\n", encoding="utf-8")
    (state / "known_failures.md").write_text("demo-0001 failed before\n", encoding="utf-8")

    config = _project_config(repo)
    task = _task_contract("demo-0001")

    worker = build_phase_context_bundle(config, task, phase=ContextPhase.WORKER)
    review = build_phase_context_bundle(config, task, phase=ContextPhase.REVIEW)
    repair = build_phase_context_bundle(config, task, phase=ContextPhase.REPAIR)

    assert worker.included_categories == [
        "active_constraints",
        "known_failures",
        "release_plan",
        "backlog_state",
    ]
    assert review.included_categories == [
        "active_constraints",
        "release_plan",
        "backlog_state",
        "known_failures",
    ]
    assert repair.included_categories == ["active_constraints", "known_failures", "backlog_state"]
    assert worker.omitted_categories == []
    assert review.omitted_categories == []
    assert repair.omitted_categories == ["release_plan"]

    for bundle in (worker, review, repair):
        assert bundle.manifest is not None
        assert bundle.total_chars == bundle.manifest.total_chars
        assert bundle.chars_by_category
        assert bundle.truncation_records == []
        payload = bundle.to_manifest_payload()
        assert isinstance(payload, dict)
        assert payload["included_categories"] == bundle.included_categories
        assert payload["total_chars"] == bundle.total_chars


def test_truncation_is_deterministic_and_recorded(tmp_path) -> None:
    repo = tmp_path / "repo"
    state = repo / "repo_state" / "demo"
    state.mkdir(parents=True)
    (state / "architecture_summary.md").write_text("A" * 10, encoding="utf-8")
    (state / "active_constraints.yaml").write_text("B" * 10, encoding="utf-8")
    (state / "backlog_state.yaml").write_text("C" * 10, encoding="utf-8")

    config = _project_config(repo)
    task = _task_contract("demo-0001")

    first = build_phase_context_bundle(config, task, phase=ContextPhase.WORKER, max_chars=15)
    second = build_phase_context_bundle(config, task, phase=ContextPhase.WORKER, max_chars=15)

    assert [section.name for section in first.sections] == ["architecture_summary", "active_constraints"]
    assert first.sections[1].content == "B" * 5
    assert [record.reason for record in first.truncation_records] == [
        "truncated_to_budget",
        "omitted_after_budget",
    ]
    assert first.to_manifest_payload() == second.to_manifest_payload()
    assert first.total_chars == second.total_chars == 15


def test_load_context_bundle_backward_compatibility(tmp_path) -> None:
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
    expected_names = ["architecture_summary", "active_constraints", "backlog_state"]
    assert [section.name for section in context.sections] == expected_names


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
