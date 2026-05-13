from __future__ import annotations

import subprocess
import threading
import time
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import yaml
import pytest

from agentic_devloop.git_finalize import FinalizeResult
from agentic_devloop.models import ExecutorResult, ProjectConfig, TaskContract
from agentic_devloop.models import Decision, Reviewer, ReviewDecision
from agentic_devloop.models import FeatureReviewDecision
from agentic_devloop.orchestrator import TaskRunResult, executor_config_for_task, executor_configs_for_task
from agentic_devloop.release import (
    collect_release_planning_state_review_snapshot,
    _completed_release_task_ids,
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
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            time.sleep(0.2)
            return super().run(prompt_path=prompt_path, worktree_path=worktree_path, output_dir=output_dir)
        finally:
            with self._lock:
                self._active -= 1


class SharedSourceExecutor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        prompt_text = prompt_path.read_text(encoding="utf-8")
        task_id = "unknown"
        if "task_id: demo-0001" in prompt_text:
            task_id = "demo-0001"
        elif "task_id: demo-0002" in prompt_text:
            task_id = "demo-0002"
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            time.sleep(0.2)
            shared = worktree_path / "src" / "shared.py"
            shared.parent.mkdir(parents=True, exist_ok=True)
            shared.write_text(f'LAST_TASK = "{task_id}"\n', encoding="utf-8")
            stdout_path = output_dir / "executor_stdout.log"
            stderr_path = output_dir / "executor_stderr.log"
            stdout_path.write_text("shared source executor\n", encoding="utf-8")
            stderr_path.write_text("", encoding="utf-8")
            return ExecutorResult(
                command=["shared-source-executor"],
                exit_code=0,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                duration_seconds=0.01,
                backend="fake",
                model=None,
            )
        finally:
            with self._lock:
                self._active -= 1


class FlakyVerificationExecutor(FakeExecutor):
    def __init__(self) -> None:
        self._attempts: dict[str, int] = {}

    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        prompt_text = prompt_path.read_text(encoding="utf-8")
        task_id = "unknown"
        if "task_id: demo-0001" in prompt_text:
            task_id = "demo-0001"
        attempts = self._attempts.get(task_id, 0) + 1
        self._attempts[task_id] = attempts

        docs_dir = worktree_path / "docs"
        if attempts == 1:
            if docs_dir.exists():
                for child in docs_dir.iterdir():
                    child.unlink()
                docs_dir.rmdir()
        else:
            output_file = docs_dir / f"{task_id}.md"
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_text(f"# {task_id}\n", encoding="utf-8")

        stdout_path = output_dir / "executor_stdout.log"
        stderr_path = output_dir / "executor_stderr.log"
        stdout_path.write_text(f"flaky verification executor attempt={attempts}\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return ExecutorResult(
            command=["flaky-verification-executor"],
            exit_code=0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            duration_seconds=0.01,
            backend="fake",
            model=None,
        )


class AllowedFilesExecutor:
    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        prompt_text = prompt_path.read_text(encoding="utf-8")
        match = prompt_text.split("```yaml\n", 1)
        if len(match) != 2:
            raise AssertionError("executor prompt missing contract yaml block")
        contract_yaml, _rest = match[1].split("```", 1)
        contract = yaml.safe_load(contract_yaml)
        allowed_files = contract.get("allowed_files") or []
        if not allowed_files:
            raise AssertionError("contract missing allowed_files")
        target = worktree_path / str(allowed_files[0])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("ok\n", encoding="utf-8")

        stdout_path = output_dir / "executor_stdout.log"
        stderr_path = output_dir / "executor_stderr.log"
        stdout_path.write_text("allowed files executor\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return ExecutorResult(
            command=["allowed-files-executor"],
            exit_code=0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            duration_seconds=0.01,
            backend="fake",
            model=None,
        )


class FlakyTaskVerificationExecutor(FakeExecutor):
    def __init__(self, task_id: str) -> None:
        self._task_id = task_id
        self._attempts = 0

    @property
    def attempts(self) -> int:
        return self._attempts

    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        prompt_text = prompt_path.read_text(encoding="utf-8")
        if f"task_id: {self._task_id}" in prompt_text:
            self._attempts += 1
            docs_dir = worktree_path / "docs"
            if self._attempts == 1:
                if docs_dir.exists():
                    for child in docs_dir.iterdir():
                        child.unlink()
                    docs_dir.rmdir()
            else:
                output_file = docs_dir / f"{self._task_id}.md"
                output_file.parent.mkdir(parents=True, exist_ok=True)
                output_file.write_text(f"# {self._task_id}\n", encoding="utf-8")
        else:
            return super().run(prompt_path=prompt_path, worktree_path=worktree_path, output_dir=output_dir)

        stdout_path = output_dir / "executor_stdout.log"
        stderr_path = output_dir / "executor_stderr.log"
        stdout_path.write_text(
            f"flaky task verification executor task={self._task_id} attempt={self._attempts}\n",
            encoding="utf-8",
        )
        stderr_path.write_text("", encoding="utf-8")
        return ExecutorResult(
            command=["flaky-task-verification-executor"],
            exit_code=0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            duration_seconds=0.01,
            backend="fake",
            model=None,
        )


class FailingExecutor:
    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = output_dir / "executor_stdout.log"
        stderr_path = output_dir / "executor_stderr.log"
        stdout_path.write_text("executor failed intentionally\n", encoding="utf-8")
        stderr_path.write_text("fatal executor error\n", encoding="utf-8")
        return ExecutorResult(
            command=["failing-executor"],
            exit_code=1,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            duration_seconds=0.01,
            backend="fake",
            model=None,
        )


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

    executor = SlowFakeExecutor()
    result = run_release(
        project_id="demo",
        release_id="v0.1.0",
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=tmp_path / "runs",
        executor=executor,
        execution_mode="parallel",
    )

    assert result.decision == Decision.ACCEPTED
    assert executor.max_active == 2
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


def test_release_dependency_map_accepts_completed_prior_release_tasks() -> None:
    tasks = [
        _task_contract("demo-0002", allowed_files=["docs/demo-0002.md"]).model_copy(
            update={"depends_on": ["demo-0001"]}
        )
    ]
    report = analyze_contract_overlaps(tasks)

    dependencies = _release_dependency_map(
        tasks,
        report,
        completed_task_ids={"demo-0001"},
    )

    assert dependencies == {}


def test_completed_release_task_ids_reads_accepted_merged_summaries(tmp_path) -> None:
    summary_dir = tmp_path / "20260512T000000Z_demo_release"
    summary_dir.mkdir(parents=True)
    (summary_dir / "release_summary.json").write_text(
        json.dumps(
            {
                "release_id": "demo",
                "integration_branch": "feature/demo",
                "tasks": [
                    {"task_id": "demo-0001", "decision": "accepted", "merged": True},
                    {"task_id": "demo-0002", "decision": "failed", "merged": True},
                    {"task_id": "demo-0003", "decision": "accepted", "merged": False},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    completed = _completed_release_task_ids(
        runs_dir=tmp_path,
        release_id="demo",
        integration_branch="feature/demo",
    )

    assert completed == {"demo-0001"}


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


def test_run_release_supervisor_repairs_verification_and_resumes(tmp_path) -> None:
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
        executor=FlakyVerificationExecutor(),
        merge_on_accept=True,
    )

    assert result.decision == Decision.ACCEPTED
    supervisor_dir = tmp_path / "runs" / result.run_id / "runtime_supervisor"
    repair_path = supervisor_dir / "repair_demo-0001.json"
    assert repair_path.exists()
    repair = json.loads(repair_path.read_text(encoding="utf-8"))
    assert repair["final_result"]["decision"] == Decision.ACCEPTED
    assert len(repair["attempts"]) == 1
    attempt = repair["attempts"][0]
    assert attempt["decision"] == "retry"
    assert attempt["classification"] == "release_resumable"
    assert attempt["action_kind"] == "release_resume"
    assert attempt["applier_applied"] is True
    assert attempt["applier_stop_evidence"] is None
    assert "event=repair_succeeded task=demo-0001 attempt=1" in result.log_path.read_text(encoding="utf-8")
    assert repair["initial_result"]["run_id"] != repair["final_result"]["run_id"]
    assert result.task_results[0].run_id == repair["final_result"]["run_id"]


def test_run_release_continues_with_completed_dependencies_during_repair_resume(tmp_path) -> None:
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
        _task_contract("demo-0002", allowed_files=["docs/demo-0002.md"]).model_copy(
            update={"depends_on": ["demo-0001"]}
        ).model_dump(mode="json"),
    )

    prior_bundle = tmp_path / "runs" / "20260512T000000Z_v0.1.0_task_demo-0001" / "task_bundle"
    prior_bundle.mkdir(parents=True, exist_ok=True)
    accepted_marker = prior_bundle / "accepted_marker.txt"
    accepted_marker.write_text("preserve-me\n", encoding="utf-8")
    prior_summary_dir = tmp_path / "runs" / "20260512T000000Z_v0.1.0_release"
    prior_summary_dir.mkdir(parents=True, exist_ok=True)
    (prior_summary_dir / "release_summary.json").write_text(
        json.dumps(
            {
                "release_id": "v0.1.0",
                "integration_branch": "feature/v0.1.0",
                "tasks": [
                    {
                        "task_id": "demo-0001",
                        "decision": "accepted",
                        "merged": True,
                        "bundle_path": str(prior_bundle),
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    executor = FlakyTaskVerificationExecutor("demo-0002")
    result = run_release(
        project_id="demo",
        release_id="v0.1.0",
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=tmp_path / "runs",
        executor=executor,
        merge_on_accept=True,
    )

    assert result.decision == Decision.ACCEPTED
    assert [task.decision.task_id for task in result.task_results] == ["demo-0002"]
    assert executor.attempts == 2
    log = result.log_path.read_text(encoding="utf-8")
    assert 'event=completed_release_tasks_skipped tasks=["demo-0001"]' in log
    assert "event=repair_succeeded task=demo-0002 attempt=1" in log
    assert accepted_marker.read_text(encoding="utf-8") == "preserve-me\n"
    assert accepted_marker.exists()


def test_run_release_accepts_completed_only_continuation(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo)
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        _task_contract("demo-0001", allowed_files=["docs/demo-0001.md"]).model_dump(mode="json"),
    )
    prior_summary_dir = tmp_path / "runs" / "20260512T000000Z_v0.1.0_release"
    prior_summary_dir.mkdir(parents=True, exist_ok=True)
    (prior_summary_dir / "release_summary.json").write_text(
        json.dumps(
            {
                "release_id": "v0.1.0",
                "integration_branch": "feature/v0.1.0",
                "tasks": [{"task_id": "demo-0001", "decision": "accepted", "merged": True}],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_release(
        project_id="demo",
        release_id="v0.1.0",
        contract_paths=[contracts_dir / "demo-0001.yaml"],
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=tmp_path / "runs",
        executor=AllowedFilesExecutor(),
        merge_on_accept=True,
    )

    assert result.decision == Decision.ACCEPTED
    assert result.task_results == []
    log = result.log_path.read_text(encoding="utf-8")
    assert 'event=completed_release_tasks_skipped tasks=["demo-0001"]' in log


def test_run_release_supervisor_stops_on_unsafe_repair(tmp_path) -> None:
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
        executor=FailingExecutor(),
    )

    assert result.decision == Decision.ESCALATED
    supervisor_dir = tmp_path / "runs" / result.run_id / "runtime_supervisor"
    repair_path = supervisor_dir / "repair_demo-0001.json"
    assert repair_path.exists()
    repair = json.loads(repair_path.read_text(encoding="utf-8"))
    assert repair["final_result"] is None
    assert len(repair["attempts"]) == 1
    attempt = repair["attempts"][0]
    assert attempt["decision"] == "stop"
    assert attempt["classification"] == "unsafe_policy_expansion"
    assert attempt["stop_reason"] == "unsafe_policy_expansion"
    assert "event=repair_stopped task=demo-0001" in result.log_path.read_text(encoding="utf-8")


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


def test_run_release_accepts_minor_budget_overage_with_soft_decision_artifact(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo, max_strong_model_calls_per_release=10)
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
                }
            ]
            * 11
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

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    review = result.review_path.read_text(encoding="utf-8")
    metrics = json.loads(result.metrics_path.read_text(encoding="utf-8"))
    soft_path = Path(summary["release_soft_gate_decision_path"])

    assert result.decision == Decision.ACCEPTED
    assert result.finalization is not None
    assert summary["budget_violations"] == []
    assert len(summary["soft_budget_findings"]) == 1
    assert "strong_model_calls_per_release exceeded budget" in summary["soft_budget_findings"][0]
    assert soft_path.exists()
    assert "Soft decision artifact" in review
    assert str(soft_path) in review
    assert metrics["soft_budget_findings"] == summary["soft_budget_findings"]


def test_run_release_rejects_severe_budget_overage(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo, max_strong_model_calls_per_release=5)
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
                }
            ]
            * 7
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

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert result.decision == Decision.FAILED
    assert result.finalization is None
    assert summary["release_soft_gate_decision_path"] is None
    assert summary["budget_violations"]
    assert summary["soft_budget_findings"] == []


def test_run_release_failed_task_remains_hard_failure_even_with_soft_budget_findings(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo, max_strong_model_calls_per_release=10)
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
                }
            ]
            * 11
        ),
        encoding="utf-8",
    )

    result = run_release(
        project_id="demo",
        release_id="v0.1.0",
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=runs_dir,
        executor=FailingExecutor(),
        merge_on_accept=True,
        release_finalize="merge-main",
    )

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert result.decision == Decision.ESCALATED
    assert result.finalization is None
    assert summary["soft_budget_findings"]
    assert summary["release_soft_gate_decision_path"] is None


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
    assert report.has_parallel_blockers is False
    assert report.findings[0].severity == "minor"


def test_analyze_contract_overlaps_treats_same_concrete_file_as_soft_finding() -> None:
    report = analyze_contract_overlaps(
        [
            _task_contract("demo-0001", allowed_files=["README.md"]),
            _task_contract("demo-0002", allowed_files=["README.md"]),
        ]
    )

    assert report.has_blocking_findings is False
    assert report.findings[0].severity == "minor"


def test_analyze_contract_overlaps_blocks_configured_unsafe_overlap_paths() -> None:
    report = analyze_contract_overlaps(
        [
            _task_contract("demo-0001", allowed_files=["src/shared.py"]),
            _task_contract("demo-0002", allowed_files=["src/shared.py"]),
        ],
        unsafe_overlap_paths=["src/shared.py"],
    )

    assert report.has_blocking_findings is True
    assert report.findings[0].severity == "blocking"


def test_run_release_parallel_serializes_same_non_unsafe_source_overlap_and_accepts(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo, verification_command="true")
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        _task_contract(
            "demo-0001",
            allowed_files=["src/shared.py"],
            verification_commands=["true"],
        ).model_dump(mode="json"),
    )
    _write_yaml(
        contracts_dir / "demo-0002.yaml",
        _task_contract(
            "demo-0002",
            allowed_files=["src/shared.py"],
            verification_commands=["true"],
        ).model_dump(mode="json"),
    )

    executor = SharedSourceExecutor()
    result = run_release(
        project_id="demo",
        release_id="v0.1.0",
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=tmp_path / "runs",
        executor=executor,
        execution_mode="parallel",
    )

    assert result.decision == Decision.ACCEPTED
    assert executor.max_active == 1
    log = result.log_path.read_text(encoding="utf-8")
    assert "Contract overlap findings: 1" in log
    assert "Execution DAG" in log


def test_run_release_rejects_configured_unsafe_overlap_paths(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo, unsafe_overlap_paths=["src/shared.py"])
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        _task_contract("demo-0001", allowed_files=["src/shared.py"]).model_dump(mode="json"),
    )
    _write_yaml(
        contracts_dir / "demo-0002.yaml",
        _task_contract("demo-0002", allowed_files=["src/shared.py"]).model_dump(mode="json"),
    )

    try:
        run_release(
            project_id="demo",
            release_id="v0.1.0",
            config_dir=config_dir,
            contracts_dir=contracts_dir,
            runs_dir=tmp_path / "runs",
            executor=SharedSourceExecutor(),
            execution_mode="parallel",
        )
    except ValueError as error:
        assert "release contracts are unsafe for parallel execution" in str(error)
    else:
        raise AssertionError("expected unsafe overlap configuration to hard-reject the release")


def test_analyze_contract_overlaps_blocks_lockfiles_and_migrations_and_out_of_scope() -> None:
    lockfile_report = analyze_contract_overlaps(
        [
            _task_contract("demo-0001", allowed_files=["poetry.lock"]),
            _task_contract("demo-0002", allowed_files=["poetry.lock"]),
        ]
    )
    migration_report = analyze_contract_overlaps(
        [
            _task_contract("demo-0001", allowed_files=["migrations/001_init.sql"]),
            _task_contract("demo-0002", allowed_files=["migrations/001_init.sql"]),
        ]
    )
    out_of_scope_report = analyze_contract_overlaps(
        [
            _task_contract("demo-0001", allowed_files=["**"]),
            _task_contract("demo-0002", allowed_files=["src/app.py"]),
        ]
    )

    assert lockfile_report.has_blocking_findings is True
    assert migration_report.has_blocking_findings is True
    assert out_of_scope_report.has_blocking_findings is True


def test_state_review_snapshot_collector_writes_deterministic_artifact(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    repo_state = repo / "repo_state" / "demo"
    repo_state.mkdir(parents=True)
    (repo_state / "architecture_summary.md").write_text("summary\n", encoding="utf-8")
    (repo_state / "active_constraints.yaml").write_text("constraints: []\n", encoding="utf-8")
    (repo_state / "backlog_state.yaml").write_text("active_goal: demo\n", encoding="utf-8")
    (repo_state / "release_plan.yaml").write_text("release_id: demo\nactive_objective: test\n", encoding="utf-8")
    (repo_state / "benchmark_status.json").write_text("{\"status\":\"none\"}\n", encoding="utf-8")
    _git(repo, "checkout", "-b", "feature/test")
    (repo / "README.md").write_text("# changed\n", encoding="utf-8")
    runs_dir = tmp_path / "runs"
    (runs_dir / "20260512T000000Z_demo_release").mkdir(parents=True)
    (runs_dir / "20260511T000000Z_demo_release").mkdir(parents=True)
    artifacts_dir = tmp_path / "planning_artifacts"
    artifacts_dir.mkdir(parents=True)

    artifact_path = collect_release_planning_state_review_snapshot(
        config_repo_path=repo,
        repo_state_path=Path("repo_state/demo"),
        runs_dir=runs_dir,
        planning_artifacts_dir=artifacts_dir,
        now=datetime(2026, 5, 13, 0, 0, tzinfo=UTC),
    )

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact_path == artifacts_dir / "state_review_snapshot.json"
    assert payload["captured_at"] == "2026-05-13T00:00:00Z"
    assert payload["branch"] == "feature/test"
    assert payload["status_lines"] == ["?? repo_state/", "M README.md"]
    assert payload["local_branches"] == ["feature/test", "main"]
    assert payload["recent_release_runs"] == [
        "20260512T000000Z_demo_release",
        "20260511T000000Z_demo_release",
    ]
    assert payload["repo_state_path"] == str(repo_state.resolve())
    assert payload["repo_state_files"] == {
        "architecture_summary": str((repo_state / "architecture_summary.md").resolve()),
        "active_constraints": str((repo_state / "active_constraints.yaml").resolve()),
        "backlog_state": str((repo_state / "backlog_state.yaml").resolve()),
        "release_plan": str((repo_state / "release_plan.yaml").resolve()),
        "benchmark_status": str((repo_state / "benchmark_status.json").resolve()),
    }


def test_state_review_snapshot_collector_handles_missing_repo_state_path(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    artifacts_dir = tmp_path / "planning_artifacts"
    artifacts_dir.mkdir(parents=True)

    artifact_path = collect_release_planning_state_review_snapshot(
        config_repo_path=repo,
        repo_state_path=None,
        runs_dir=tmp_path / "runs",
        planning_artifacts_dir=artifacts_dir,
        now=datetime(2026, 5, 13, 0, 0, tzinfo=UTC),
    )

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["repo_state_path"] is None
    assert payload["repo_state_files"] == {
        "architecture_summary": None,
        "active_constraints": None,
        "backlog_state": None,
        "release_plan": None,
        "benchmark_status": None,
    }


def test_state_review_snapshot_collector_requires_existing_planning_artifacts_dir(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    missing_dir = tmp_path / "missing-artifacts"

    try:
        collect_release_planning_state_review_snapshot(
            config_repo_path=repo,
            repo_state_path=None,
            runs_dir=tmp_path / "runs",
            planning_artifacts_dir=missing_dir,
            now=datetime(2026, 5, 13, 0, 0, tzinfo=UTC),
        )
    except ValueError as error:
        assert "release planning artifacts directory does not exist" in str(error)
    else:
        raise AssertionError("expected missing planning artifacts directory to fail")


def test_run_release_feature_review_repair_loop_records_artifacts(tmp_path: Path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
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
            "model_roles": {
                "worker": {
                    "type": "codex_cli",
                    "model": "gpt-5.3-codex-spark",
                    "max_walltime_minutes": 5,
                },
                "reviewer": {
                    "type": "codex_cli",
                    "model": "gpt-5.3-codex-spark",
                    "max_walltime_minutes": 5,
                },
            },
            "model_routing": {"default_role": "worker"},
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

    decisions = [
        FeatureReviewDecision.model_validate(
            {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Needs a repair.",
                "recommendation": "require_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "finding-1",
                        "severity": "high",
                        "summary": "Fix required.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Update docs."],
                        "optional_follow_ups": [],
                    }
                ],
            }
        ),
        FeatureReviewDecision.model_validate(
            {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Repairs applied.",
                "recommendation": "approve",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [],
            }
        ),
    ]

    class FakeBackendResult:
        def __init__(self, decision: FeatureReviewDecision) -> None:
            self.decision = decision

    def fake_invoke_feature_reviewer(*_args, **_kwargs):
        if not decisions:
            raise AssertionError("unexpected reviewer invocation")
        return FakeBackendResult(decisions.pop(0))

    with patch("agentic_devloop.release.invoke_feature_reviewer", side_effect=fake_invoke_feature_reviewer):
        result = run_release(
            project_id="demo",
            release_id="v0.1.0",
            config_dir=config_dir,
            contracts_dir=contracts_dir,
            runs_dir=tmp_path / "runs",
            executor=AllowedFilesExecutor(),
            merge_on_accept=True,
        )

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert result.decision == Decision.ACCEPTED
    assert summary["feature_review_path"] is not None
    assert summary["feature_review_recheck_path"] is not None
    recheck = json.loads(Path(summary["feature_review_recheck_path"]).read_text(encoding="utf-8"))
    assert recheck["stop_reason"] == "resolved"


def test_run_release_feature_review_uses_all_release_contracts_for_continuation_repairs(tmp_path: Path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    (repo / "src" / "agentic_devloop").mkdir(parents=True)
    (repo / "src" / "agentic_devloop" / "feature_review.py").write_text("ORIGINAL = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add source"], cwd=repo, check=True)

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {"type": "codex_cli", "model": "gpt-5.3-codex-spark", "max_walltime_minutes": 5},
            "model_roles": {
                "worker": {"type": "codex_cli", "model": "gpt-5.3-codex-spark", "max_walltime_minutes": 5},
                "reviewer": {"type": "codex_cli", "model": "gpt-5.3-codex-spark", "max_walltime_minutes": 5},
            },
            "model_routing": {"default_role": "worker"},
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
    _write_yaml(
        contracts_dir / "demo-0002.yaml",
        _task_contract(
            "demo-0002",
            allowed_files=["src/agentic_devloop/feature_review.py"],
            verification_commands=["test -f src/agentic_devloop/feature_review.py"],
        ).model_dump(mode="json"),
    )

    decisions = [
        FeatureReviewDecision.model_validate(
            {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Needs continuation-scope repair.",
                "recommendation": "require_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "finding-continuation-1",
                        "severity": "high",
                        "summary": "Fix source from earlier slice.",
                        "affected_files": ["src/agentic_devloop/feature_review.py"],
                        "required_repairs": ["Update feature review implementation."],
                        "optional_follow_ups": [],
                    }
                ],
            }
        ),
        FeatureReviewDecision.model_validate(
            {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Repairs applied.",
                "recommendation": "approve",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [],
            }
        ),
    ]

    class FakeBackendResult:
        def __init__(self, decision: FeatureReviewDecision) -> None:
            self.decision = decision

    def fake_invoke_feature_reviewer(*_args, **_kwargs):
        if not decisions:
            raise AssertionError("unexpected reviewer invocation")
        return FakeBackendResult(decisions.pop(0))

    with patch("agentic_devloop.release.invoke_feature_reviewer", side_effect=fake_invoke_feature_reviewer):
        result = run_release(
            project_id="demo",
            release_id="v0.1.0",
            config_dir=config_dir,
            contracts_dir=contracts_dir,
            contract_paths=[contracts_dir / "demo-0001.yaml"],
            runs_dir=tmp_path / "runs",
            executor=AllowedFilesExecutor(),
            merge_on_accept=True,
        )

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    recheck = json.loads(Path(summary["feature_review_recheck_path"]).read_text(encoding="utf-8"))
    assert result.decision == Decision.ACCEPTED
    assert recheck["stop_reason"] == "resolved"
    assert not decisions


def test_run_release_feature_review_reruns_requested_verification_subset(tmp_path: Path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {"type": "codex_cli", "model": "gpt-5.3-codex-spark", "max_walltime_minutes": 5},
            "model_roles": {
                "worker": {"type": "codex_cli", "model": "gpt-5.3-codex-spark", "max_walltime_minutes": 5},
                "reviewer": {"type": "codex_cli", "model": "gpt-5.3-codex-spark", "max_walltime_minutes": 5},
            },
            "model_routing": {"default_role": "worker"},
            "verification_profiles": {"default": {"commands": ["test -d docs", "test -f README.md"]}},
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

    decisions = [
        FeatureReviewDecision.model_validate(
            {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Needs a repair.",
                "recommendation": "require_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": ["test -d docs"],
                "findings": [
                    {
                        "finding_id": "finding-1",
                        "severity": "high",
                        "summary": "Fix required.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Update docs."],
                        "optional_follow_ups": [],
                    }
                ],
            }
        ),
        FeatureReviewDecision.model_validate(
            {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Repairs applied.",
                "recommendation": "approve",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [],
            }
        ),
    ]

    class FakeBackendResult:
        def __init__(self, decision: FeatureReviewDecision) -> None:
            self.decision = decision

    def fake_invoke_feature_reviewer(*_args, **_kwargs):
        if not decisions:
            raise AssertionError("unexpected reviewer invocation")
        return FakeBackendResult(decisions.pop(0))

    with patch("agentic_devloop.release.invoke_feature_reviewer", side_effect=fake_invoke_feature_reviewer), patch(
        "agentic_devloop.release._run_integration_verification_rerun",
        return_value=True,
    ) as rerun:
        result = run_release(
            project_id="demo",
            release_id="v0.1.0",
            config_dir=config_dir,
            contracts_dir=contracts_dir,
            runs_dir=tmp_path / "runs",
            executor=AllowedFilesExecutor(),
            merge_on_accept=True,
        )

    assert result.decision == Decision.ACCEPTED
    assert rerun.call_args.kwargs["commands"] == ["test -d docs"]


def test_run_release_feature_review_repair_recheck_allows_finalization_gate(tmp_path: Path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
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
            "model_roles": {
                "worker": {
                    "type": "codex_cli",
                    "model": "gpt-5.3-codex-spark",
                    "max_walltime_minutes": 5,
                },
                "reviewer": {
                    "type": "codex_cli",
                    "model": "gpt-5.3-codex-spark",
                    "max_walltime_minutes": 5,
                },
            },
            "model_routing": {"default_role": "worker"},
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

    decisions = [
        FeatureReviewDecision.model_validate(
            {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Needs a repair.",
                "recommendation": "require_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "finding-1",
                        "severity": "high",
                        "summary": "Fix required.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Update docs."],
                        "optional_follow_ups": [],
                    }
                ],
            }
        ),
        FeatureReviewDecision.model_validate(
            {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Repairs applied.",
                "recommendation": "approve",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [],
            }
        ),
    ]

    class FakeBackendResult:
        def __init__(self, decision: FeatureReviewDecision) -> None:
            self.decision = decision

    def fake_invoke_feature_reviewer(*_args, **_kwargs):
        if not decisions:
            raise AssertionError("unexpected reviewer invocation")
        return FakeBackendResult(decisions.pop(0))

    with patch("agentic_devloop.release.invoke_feature_reviewer", side_effect=fake_invoke_feature_reviewer):
        result = run_release(
            project_id="demo",
            release_id="v0.1.0",
            config_dir=config_dir,
            contracts_dir=contracts_dir,
            runs_dir=tmp_path / "runs",
            executor=AllowedFilesExecutor(),
            merge_on_accept=True,
            release_finalize="merge-main",
        )

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    recheck = json.loads(Path(summary["feature_review_recheck_path"]).read_text(encoding="utf-8"))
    review = result.review_path.read_text(encoding="utf-8")

    assert result.decision == Decision.ACCEPTED
    assert result.finalization is not None
    assert result.finalization.merged is True
    assert result.finalization_gate is not None
    assert result.finalization_gate["allowed"] is True
    assert result.finalization_gate["reason"] == "allowed"
    assert result.finalization_gate["unresolved_required_finding_ids"] == []
    assert summary["finalization_gate"]["allowed"] is True
    assert summary["finalization_gate"]["reason"] == "allowed"
    assert summary["finalization_gate"]["unresolved_required_finding_ids"] == []
    assert summary["feature_review_recheck_path"] is not None
    assert recheck["stop_reason"] == "resolved"
    assert not decisions
    assert "- Gate reason: `allowed`" in review
    assert "- Unresolved required findings: `0`" in review


def test_run_release_feature_review_end_to_end_flow_opens_finalization_gate(tmp_path: Path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
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
            "model_roles": {
                "worker": {
                    "type": "codex_cli",
                    "model": "gpt-5.3-codex-spark",
                    "max_walltime_minutes": 5,
                },
                "reviewer": {
                    "type": "codex_cli",
                    "model": "gpt-5.3-codex-spark",
                    "max_walltime_minutes": 5,
                },
            },
            "model_routing": {"default_role": "worker"},
            "verification_profiles": {"default": {"commands": ["test -d docs", "test -f README.md"]}},
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

    decisions = [
        FeatureReviewDecision.model_validate(
            {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Repair needed.",
                "recommendation": "require_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": ["test -d docs"],
                "findings": [
                    {
                        "finding_id": "finding-1",
                        "severity": "high",
                        "summary": "Fix required.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Update docs."],
                        "optional_follow_ups": [],
                    }
                ],
            }
        ),
        FeatureReviewDecision.model_validate(
            {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Repair verified.",
                "recommendation": "approve",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [],
            }
        ),
    ]

    class FakeBackendResult:
        def __init__(self, decision: FeatureReviewDecision) -> None:
            self.decision = decision

    def fake_invoke_feature_reviewer(*_args, **_kwargs):
        if not decisions:
            raise AssertionError("unexpected reviewer invocation")
        return FakeBackendResult(decisions.pop(0))

    with patch("agentic_devloop.release.invoke_feature_reviewer", side_effect=fake_invoke_feature_reviewer), patch(
        "agentic_devloop.release._run_integration_verification_rerun",
        return_value=True,
    ) as rerun:
        result = run_release(
            project_id="demo",
            release_id="v0.1.0",
            config_dir=config_dir,
            contracts_dir=contracts_dir,
            runs_dir=tmp_path / "runs",
            executor=AllowedFilesExecutor(),
            merge_on_accept=True,
            release_finalize="merge-main",
        )

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert result.decision == Decision.ACCEPTED
    assert result.finalization is not None
    assert result.finalization.merged is True
    assert result.finalization_gate is not None
    assert result.finalization_gate["allowed"] is True
    assert summary["feature_review_path"] is not None
    assert summary["feature_review_recheck_path"] is not None
    assert summary["finalization_gate"]["allowed"] is True
    assert rerun.call_count == 1
    assert rerun.call_args.kwargs["commands"] == ["test -d docs"]


def test_run_release_blocks_finalization_on_unresolved_required_feature_review_findings(tmp_path: Path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
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
            "model_roles": {
                "worker": {
                    "type": "codex_cli",
                    "model": "gpt-5.3-codex-spark",
                    "max_walltime_minutes": 5,
                },
                "reviewer": {
                    "type": "codex_cli",
                    "model": "gpt-5.3-codex-spark",
                    "max_walltime_minutes": 5,
                },
            },
            "model_routing": {"default_role": "worker"},
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

    decision = FeatureReviewDecision.model_validate(
        {
            "release_id": "v0.1.0",
            "reviewer": "strong_model",
            "summary": "Required repair cannot be mapped to a bounded repair contract.",
            "recommendation": "require_repairs",
            "accepted_risks": [],
            "rerun_verification_commands": [],
            "findings": [
                {
                    "finding_id": "finding-required-1",
                    "severity": "high",
                    "summary": "Fix required in file outside allowed contract scope.",
                    "affected_files": ["src/outside_scope.py"],
                    "required_repairs": ["Apply a code fix."],
                    "optional_follow_ups": [],
                }
            ],
        }
    )

    class FakeBackendResult:
        def __init__(self, decision: FeatureReviewDecision) -> None:
            self.decision = decision

    with patch("agentic_devloop.release.invoke_feature_reviewer", return_value=FakeBackendResult(decision)), patch(
        "agentic_devloop.release.generate_repair_contracts_for_required_findings",
        return_value=[],
    ):
        result = run_release(
            project_id="demo",
            release_id="v0.1.0",
            config_dir=config_dir,
            contracts_dir=contracts_dir,
            runs_dir=tmp_path / "runs",
            executor=AllowedFilesExecutor(),
            merge_on_accept=True,
            release_finalize="merge-main",
        )

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    review = result.review_path.read_text(encoding="utf-8")

    assert result.finalization is None
    assert result.finalization_gate is not None
    assert result.finalization_gate["allowed"] is False
    assert result.finalization_gate["reason"] == "unresolved_required_findings"
    assert result.finalization_gate["unresolved_required_finding_ids"] == ["finding-required-1"]
    assert summary["finalization_gate"]["reason"] == "unresolved_required_findings"
    assert summary["finalization_gate"]["unresolved_required_finding_ids"] == ["finding-required-1"]
    assert "- Gate reason: `unresolved_required_findings`" in review
    assert "- Unresolved required findings: `1`" in review


def _task_contract(
    task_id: str,
    budget_class: str = "S",
    allowed_files: list[str] | None = None,
    verification_commands: list[str] | None = None,
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
            "verification": {"commands": verification_commands or ["test -d docs"]},
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
    verification_command: str = "test -d docs",
    unsafe_overlap_paths: list[str] | None = None,
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
            "verification_profiles": {"default": {"commands": [verification_command]}},
            "unsafe_overlap_paths": unsafe_overlap_paths or [],
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
