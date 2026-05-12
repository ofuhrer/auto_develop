from __future__ import annotations

import subprocess
import time
import json
from pathlib import Path

import yaml

from agentic_devloop.git_finalize import FinalizeResult
from agentic_devloop.models import ExecutorResult, ProjectConfig, TaskContract
from agentic_devloop.models import Decision, Reviewer, ReviewDecision
from agentic_devloop.orchestrator import TaskRunResult, executor_config_for_task, executor_configs_for_task
from agentic_devloop.release import (
    _ensure_no_existing_task_branches,
    _ensure_no_existing_worktrees,
    _multiplexed_progress,
    _release_dependency_map,
    _should_preserve_task_branch,
    _should_preserve_task_worktree,
    analyze_contract_overlaps,
    run_release,
)


class FakeExecutor:
    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        task_id = "unknown"
        prompt_text = prompt_path.read_text(encoding="utf-8")
        if "task_id: demo-0001" in prompt_text:
            task_id = "demo-0001"
        elif "task_id: demo-0002" in prompt_text:
            task_id = "demo-0002"

        output_file = worktree_path / "docs" / f"{task_id}.md"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(f"# {task_id}\n", encoding="utf-8")

        stdout_path = output_dir / "executor_stdout.log"
        stderr_path = output_dir / "executor_stderr.log"
        stdout_path.write_text("release fake executor\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")

        return ExecutorResult(
            command=["fake-executor"],
            exit_code=0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            duration_seconds=0.01,
            backend="fake",
            model=None,
        )


class SlowFakeExecutor(FakeExecutor):
    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        time.sleep(0.2)
        return super().run(prompt_path=prompt_path, worktree_path=worktree_path, output_dir=output_dir)


def test_executor_config_for_task_uses_budget_then_task_type_roles() -> None:
    config = ProjectConfig.model_validate(
        {
            "project_id": "demo",
            "repo_path": "/tmp/demo",
            "default_base_branch": "main",
            "worktree_root": "/tmp/worktrees",
            "executor": {"type": "codex_cli", "model": "fallback", "max_walltime_minutes": 5},
            "model_roles": {
                "worker": {"type": "codex_cli", "model": "cheap", "max_walltime_minutes": 5},
                "reviewer": {"type": "codex_cli", "model": "expensive", "max_walltime_minutes": 5},
            },
            "model_routing": {
                "default_role": "worker",
                "task_type_roles": {"documentation": "worker"},
                "budget_class_roles": {"L": "reviewer"},
                "escalation_role": "reviewer",
            },
            "verification_profiles": {"default": {"commands": ["true"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        }
    )
    task = _task_contract("demo-0001", budget_class="L")

    assert executor_config_for_task(config, task).model == "expensive"


def test_executor_configs_for_task_includes_fallback_models() -> None:
    config = ProjectConfig.model_validate(
        {
            "project_id": "demo",
            "repo_path": "/tmp/demo",
            "default_base_branch": "main",
            "worktree_root": "/tmp/worktrees",
            "executor": {
                "type": "codex_cli",
                "model": "fallback",
                "fallback_models": ["fallback-2"],
                "max_walltime_minutes": 5,
            },
            "verification_profiles": {"default": {"commands": ["true"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        }
    )

    assert [executor.model for executor in executor_configs_for_task(config, _task_contract("demo-0001"))] == [
        "fallback",
        "fallback-2",
    ]


def test_run_release_executes_ordered_contracts_and_writes_summary(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "repo_state_path": "repo_state/demo",
            "executor": {
                "type": "codex_cli",
                "model": "gpt-5.3-codex-spark",
                "max_walltime_minutes": 5,
            },
            "verification_profiles": {"default": {"commands": ["test -d docs"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )
    repo_state = repo / "repo_state" / "demo"
    repo_state.mkdir(parents=True)
    _write_yaml(
        repo_state / "release_plan.yaml",
        {
            "release_id": "v0.1.0",
            "active_objective": "Run two docs tasks.",
            "current_tasks": ["demo-0001", "demo-0002"],
        },
    )
    _git(repo, "add", "repo_state/demo/release_plan.yaml")
    _git(repo, "commit", "-m", "add release plan")

    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        _task_contract("demo-0001", allowed_files=["docs/demo-0001.md"]).model_dump(mode="json"),
    )
    _write_yaml(
        contracts_dir / "demo-0002.yaml",
        _task_contract("demo-0002", allowed_files=["docs/demo-0002.md"]).model_dump(mode="json"),
    )

    result = run_release(
        project_id="demo",
        release_id="v0.1.0",
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=tmp_path / "runs",
        executor=FakeExecutor(),
        merge_on_accept=True,
    )

    assert result.decision == "accepted"
    assert [task.decision.task_id for task in result.task_results] == ["demo-0001", "demo-0002"]
    assert result.log_path.exists()
    log = result.log_path.read_text(encoding="utf-8")
    assert "📡 Watching:" in log
    assert "🧭 Task 1/2 demo-0001:" in log
    assert "🧾 Release Summary" in log
    summary = result.summary_path.read_text(encoding="utf-8")
    assert '"release_id": "v0.1.0"' in summary
    assert '"log_path":' in summary
    assert '"task_id": "demo-0001"' in summary
    assert '"task_id": "demo-0002"' in summary
    assert not result.task_results[0].worktree_path.exists()
    assert not result.task_results[1].worktree_path.exists()
    assert not any((tmp_path / "worktrees").iterdir())
    branches = subprocess.run(
        ["git", "branch", "--list", "agent/v0.1.0/*"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert branches.strip() == ""
    assert _git_output(repo, "rev-parse", "--verify", "feature/v0.1.0")
    assert _git_output(repo, "branch", "--show-current").strip() == "feature/v0.1.0"
    assert not _git_object_exists(repo, "main:docs/demo-0001.md")
    assert _git_output(repo, "show", "feature/v0.1.0:docs/demo-0001.md").strip() == "# demo-0001"
    assert result.review_path.exists()
    assert "Release Review" in result.review_path.read_text(encoding="utf-8")


def test_run_release_parallel_executes_independent_tasks_concurrently(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo)
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        _task_contract("demo-0001", allowed_files=["docs/demo-0001.md"]).model_dump(mode="json"),
    )
    _write_yaml(
        contracts_dir / "demo-0002.yaml",
        _task_contract("demo-0002", allowed_files=["docs/demo-0002.md"]).model_dump(mode="json"),
    )

    started = time.monotonic()
    result = run_release(
        project_id="demo",
        release_id="v0.1.0",
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=tmp_path / "runs",
        executor=SlowFakeExecutor(),
        execution_mode="parallel",
    )
    elapsed = time.monotonic() - started

    assert result.decision == Decision.ACCEPTED
    assert elapsed < 0.7
    assert "parallel_scheduler" in result.log_path.read_text(encoding="utf-8")
    assert result.review_path.exists()


def test_release_dependency_map_chains_explicit_and_overlapping_tasks() -> None:
    tasks = [
        _task_contract("demo-0001", allowed_files=["docs/guides/**"]),
        _task_contract("demo-0002", allowed_files=["docs/guides/setup.md"]),
        _task_contract("demo-0003", allowed_files=["docs/other.md"]),
    ]
    tasks[2] = tasks[2].model_copy(update={"depends_on": ["demo-0002"]})
    report = analyze_contract_overlaps(tasks)

    dependencies = _release_dependency_map(tasks, report)

    assert dependencies == {"demo-0002": ["demo-0001"], "demo-0003": ["demo-0002"]}


def test_multiplexed_progress_filters_noisy_agent_lines_and_keeps_raw_log(tmp_path) -> None:
    visible: list[str] = []
    progress = _multiplexed_progress(visible.append, tmp_path / "release.log", tmp_path / "raw.log")

    progress("agent task=x phase=executor attempt=1 stream=stderr | 2026 WARN plugin noise")
    progress("agent task=x phase=executor attempt=1 stream=stderr | ERROR: quota")
    progress("agent task=x phase=executor attempt=1 stream=stdout | Changed files:")
    progress("agent task=x phase=executor attempt=1 stream=stdout | - /Users/fuhrer/Desktop/auto_develop/worktrees/run/src/file.py")
    progress("agent task=x phase=executor attempt=1 stream=stdout | Result:")
    progress("agent task=x phase=executor attempt=1 stream=stdout | - Implemented useful behavior")
    progress("event=executor_heartbeat task=x phase=executor attempt=1 model=gpt-5.4-mini elapsed_seconds=120")

    log = (tmp_path / "release.log").read_text(encoding="utf-8")
    raw = (tmp_path / "raw.log").read_text(encoding="utf-8")

    assert not any("ERROR: quota" in line for line in visible)
    assert "plugin noise" not in log
    assert "ERROR: quota" not in log
    assert "📝 x worker summary: Files changed" in log
    assert "…/worktrees/run/src/file.py" in log
    assert "📝 x worker summary: Result" in log
    assert "still working after 2m 0s" in log
    assert "plugin noise" in raw
    assert "ERROR: quota" in raw


def test_release_preflight_rejects_existing_project_worktrees(tmp_path) -> None:
    worktree_root = tmp_path / "worktrees"
    stale = worktree_root / "stale-run"
    stale.mkdir(parents=True)
    (worktree_root / ".DS_Store").write_text("ignored", encoding="utf-8")

    try:
        _ensure_no_existing_worktrees(worktree_root)
    except ValueError as error:
        assert "project worktree root is not clean" in str(error)
        assert str(stale) in str(error)
    else:
        raise AssertionError("expected stale worktree preflight failure")


def test_run_release_can_finalize_feature_branch_into_main(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo)
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        _task_contract("demo-0001", allowed_files=["docs/demo-0001.md"]).model_dump(mode="json"),
    )

    result = run_release(
        project_id="demo",
        release_id="v0.1.0",
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=tmp_path / "runs",
        executor=FakeExecutor(),
        merge_on_accept=True,
        release_finalize="merge-main",
    )

    assert result.integration_branch == "feature/v0.1.0"
    assert result.finalization is not None
    assert result.finalization.merged is True
    assert (repo / "docs" / "demo-0001.md").exists()
    assert _git_output(repo, "branch", "--show-current").strip() == "main"


def test_run_release_writes_metrics_and_final_log_summary(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo)
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        _task_contract("demo-0001", allowed_files=["docs/demo-0001.md"]).model_dump(mode="json"),
    )

    result = run_release(
        project_id="demo",
        release_id="v0.1.0",
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=tmp_path / "runs",
        executor=FakeExecutor(),
        merge_on_accept=True,
    )

    metrics = json.loads(result.metrics_path.read_text(encoding="utf-8"))
    budget = json.loads(result.budget_path.read_text(encoding="utf-8"))
    tuning = result.tuning_path.read_text(encoding="utf-8")
    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    review = result.review_path.read_text(encoding="utf-8")
    log = result.log_path.read_text(encoding="utf-8")

    assert metrics["release_id"] == "v0.1.0"
    assert metrics["strong_model_calls"] == 0
    assert metrics["totals"]["tasks"] == 1
    assert metrics["totals"]["accepted_tasks"] == 1
    assert metrics["totals"]["executor_attempts"] == 1
    assert metrics["tasks"][0]["context_chars"] >= 0
    assert budget["release_id"] == "v0.1.0"
    assert any(entry["name"] == "strong_model_calls_per_release" for entry in budget["usage"])
    assert "Budget tuning guidance for v0.1.0" in tuning
    assert summary["metrics_path"] == str(result.metrics_path)
    assert summary["budget_path"] == str(result.budget_path)
    assert summary["tuning_path"] == str(result.tuning_path)
    assert str(result.budget_path) in review
    assert str(result.tuning_path) in review
    assert "🧾 Release Summary" in log
    assert "Release:" in log
    assert "v0.1.0" in log
    assert str(result.budget_path) in log
    assert str(result.tuning_path) in log
    assert "Good luck, future humans. 🧑‍🚀🛠️🍀" in log


def test_run_release_fails_when_release_budget_is_exceeded(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo, max_strong_model_calls_per_release=1)
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        _task_contract("demo-0001", allowed_files=["docs/demo-0001.md"]).model_dump(mode="json"),
    )
    runs_dir = tmp_path / "runs"
    ledger_path = runs_dir / "v0.1.0" / "budget_ledger.json"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        json.dumps(
            [
                {
                    "release_id": "v0.1.0",
                    "kind": "strong_model",
                    "model": "gpt-5.3-codex-spark",
                    "reason": "planner",
                    "created_at": "2026-05-12T00:00:00+00:00",
                },
                {
                    "release_id": "v0.1.0",
                    "kind": "strong_model",
                    "model": "gpt-5.3-codex-spark",
                    "reason": "planner-review",
                    "created_at": "2026-05-12T00:01:00+00:00",
                },
            ]
        ),
        encoding="utf-8",
    )

    result = run_release(
        project_id="demo",
        release_id="v0.1.0",
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=runs_dir,
        executor=FakeExecutor(),
        merge_on_accept=True,
        release_finalize="merge-main",
    )

    metrics = json.loads(result.metrics_path.read_text(encoding="utf-8"))
    budget = json.loads(result.budget_path.read_text(encoding="utf-8"))
    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    review = result.review_path.read_text(encoding="utf-8")

    assert result.decision == Decision.FAILED
    assert result.finalization is None
    assert not _git_object_exists(repo, "main:docs/demo-0001.md")
    assert metrics["decision"] == Decision.FAILED
    assert metrics["strong_model_calls"] == 2
    usage = {entry["name"]: entry for entry in budget["usage"]}
    assert usage["strong_model_calls_per_release"]["over_by"] == 1
    assert "strong_model_calls_per_release exceeded budget" in summary["budget_violations"][0]
    assert "Violation: strong_model_calls_per_release exceeded budget" in review


def test_release_preflight_ignores_metadata_files(tmp_path) -> None:
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    (worktree_root / ".DS_Store").write_text("ignored", encoding="utf-8")

    _ensure_no_existing_worktrees(worktree_root)


def test_release_preflight_rejects_existing_task_branches(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "branch", "agent/v0.1.0/demo-0001")

    try:
        _ensure_no_existing_task_branches(
            repo,
            "v0.1.0",
            [_task_contract("demo-0001", allowed_files=["docs/demo-0001.md"])],
        )
    except ValueError as error:
        assert "release task branches already exist" in str(error)
        assert "agent/v0.1.0/demo-0001" in str(error)
    else:
        raise AssertionError("expected stale branch preflight failure")


def test_run_release_preserves_accepted_unfinalized_worktree(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {
                "type": "codex_cli",
                "model": "gpt-5.3-codex-spark",
                "max_walltime_minutes": 5,
            },
            "verification_profiles": {"default": {"commands": ["test -d docs"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )

    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        _task_contract("demo-0001", allowed_files=["docs/demo-0001.md"]).model_dump(mode="json"),
    )

    result = run_release(
        project_id="demo",
        release_id="v0.1.0",
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=tmp_path / "runs",
        executor=FakeExecutor(),
    )

    task_result = result.task_results[0]
    assert task_result.decision.decision == Decision.ACCEPTED
    assert task_result.finalize is None
    assert task_result.worktree_path.exists()
    assert (task_result.worktree_path / "docs" / "demo-0001.md").exists()
    log = result.log_path.read_text(encoding="utf-8")
    assert f"preserved_worktree={task_result.worktree_path}" in log
    assert "preserved_branch=agent/v0.1.0/demo-0001" in log


def test_run_release_preserves_committed_unmerged_task_branch(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {
                "type": "codex_cli",
                "model": "gpt-5.3-codex-spark",
                "max_walltime_minutes": 5,
            },
            "verification_profiles": {"default": {"commands": ["test -d docs"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )

    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        _task_contract("demo-0001", allowed_files=["docs/demo-0001.md"]).model_dump(mode="json"),
    )

    result = run_release(
        project_id="demo",
        release_id="v0.1.0",
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=tmp_path / "runs",
        executor=FakeExecutor(),
        commit_on_accept=True,
    )

    finalize = result.task_results[0].finalize
    assert finalize is not None
    assert finalize.commit_hash is not None
    assert finalize.merged is False
    assert not result.task_results[0].worktree_path.exists()

    branch = "agent/v0.1.0/demo-0001"
    branch_commit = subprocess.run(
        ["git", "rev-parse", branch],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch_commit == finalize.commit_hash
    assert f"preserved_branch={branch}" in result.log_path.read_text(encoding="utf-8")


def test_failed_finalization_preserves_task_branch() -> None:
    result = TaskRunResult(
        run_id="run-1",
        worktree_path=Path("/tmp/worktree"),
        bundle_path=Path("/tmp/bundle"),
        decision=ReviewDecision(
            task_id="demo-0001",
            decision=Decision.ESCALATED,
            reviewer=Reviewer.DETERMINISTIC,
            rationale="finalization failed",
        ),
        finalize=FinalizeResult(failed_step="merge", error="conflict"),
    )

    assert _should_preserve_task_branch(result) is True
    assert _should_preserve_task_worktree(result) is True


def test_analyze_contract_overlaps_classifies_shared_scope_as_minor() -> None:
    report = analyze_contract_overlaps(
        [
            _task_contract("demo-0001", allowed_files=["docs/guides/**"]),
            _task_contract("demo-0002", allowed_files=["docs/guides/setup.md"]),
        ]
    )

    assert report.has_blocking_findings is False
    assert report.has_parallel_blockers is False
    assert report.findings[0].severity == "minor"
    assert report.findings[0].pattern == "docs/guides/** <-> docs/guides/setup.md"


def test_analyze_contract_overlaps_classifies_broad_scope_as_parallel_blocker() -> None:
    report = analyze_contract_overlaps(
        [
            _task_contract("demo-0001", allowed_files=["src/**"]),
            _task_contract("demo-0002", allowed_files=["src/agentic_devloop/release.py"]),
        ]
    )

    assert report.has_blocking_findings is False
    assert report.has_parallel_blockers is True
    assert report.findings[0].severity == "broad"


def test_analyze_contract_overlaps_blocks_same_concrete_file() -> None:
    report = analyze_contract_overlaps(
        [
            _task_contract("demo-0001", allowed_files=["README.md"]),
            _task_contract("demo-0002", allowed_files=["README.md"]),
        ]
    )

    assert report.has_blocking_findings is True
    assert report.findings[0].severity == "blocking"


def _task_contract(
    task_id: str,
    budget_class: str = "S",
    allowed_files: list[str] | None = None,
) -> TaskContract:
    return TaskContract.model_validate(
        {
            "task_id": task_id,
            "release_id": "v0.1.0",
            "title": f"Create {task_id} docs",
            "task_type": "documentation",
            "budget_class": budget_class,
            "objective": f"Create docs for {task_id}.",
            "allowed_files": allowed_files or ["docs/**"],
            "forbidden_changes": [],
            "required_evidence": ["git diff", "test output"],
            "verification": {"commands": ["test -d docs"]},
            "stop_conditions": ["Verification fails twice."],
        }
    )


def _git(repo, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _git_output(repo, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _git_object_exists(repo, ref: str) -> bool:
    return (
        subprocess.run(
            ["git", "cat-file", "-e", ref],
            cwd=repo,
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def _repo_with_initial_commit(repo: Path) -> Path:
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


def _write_demo_config(
    tmp_path: Path,
    repo: Path,
    *,
    max_strong_model_calls_per_release: int = 10,
) -> Path:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {
                "type": "codex_cli",
                "model": "gpt-5.3-codex-spark",
                "max_walltime_minutes": 5,
            },
            "verification_profiles": {"default": {"commands": ["test -d docs"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": max_strong_model_calls_per_release,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )
    return config_dir


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
