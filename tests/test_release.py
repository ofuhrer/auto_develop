from __future__ import annotations

import subprocess
import threading
import time
import json
from hashlib import sha256
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import yaml
import pytest
from pydantic import ValidationError

from agentic_devloop.git_finalize import FinalizeResult
from agentic_devloop.models import (
    ExecutorResult,
    FinalReviewContinuationDecision,
    FinalReviewContinuationOutcome,
    ProjectConfig,
    TaskContract,
)
from agentic_devloop.models import Decision, Reviewer, ReviewDecision
from agentic_devloop.models import FeatureReviewDecision, FeatureReviewRecheckRecord
from agentic_devloop.orchestrator import TaskRunResult, executor_config_for_task, executor_configs_for_task
from agentic_devloop.release import (
    _build_release_metrics,
    _cost_runtime_governance_decision_path,
    _cost_runtime_governance_feature_review_max_repair_loops_override,
    _load_or_build_cost_runtime_governance_decision,
    collect_release_planning_state_review_snapshot,
    make_release_run_id,
    _assert_safe_final_integration_verification_worktree,
    _assert_safe_feature_review_rerun_worktree,
    _build_release_finalization_gate,
    _completed_release_task_ids,
    _ensure_no_existing_task_branches,
    _ensure_no_existing_worktrees,
    _command_with_env_prefixes,
    _is_verification_only_or_conditional_finding,
    _runtime_supervisor_classification_for_task_result,
    _multiplexed_progress,
    _persist_compact_final_review_follow_up_memory,
    _release_dependency_map,
    _should_preserve_task_branch,
    _should_preserve_task_worktree,
    _write_final_review_continuation_decision,
    analyze_contract_overlaps,
    run_release,
)
from agentic_devloop.supervisor_decisions import (
    CostRuntimeGovernanceAction,
    CostRuntimeGovernanceDecision,
    EnvironmentRepairDecision,
    EnvironmentRepairPolicyAction,
    FinalReviewFindingAdjudicationDecision,
    FeatureReviewFindingClassificationDecision,
    ReleaseSchedulingAction,
    ScopeRiskAction,
    ScopeRiskAffectedScope,
    ScopeRiskBudgetPolicyDecision,
    ScopeRiskClassification,
    ScopeRiskOutcome,
    SupervisorDecisionType,
    load_supervisor_decision_artifact,
    supervisor_decision_artifact_path,
    write_supervisor_decision_artifact,
)
from agentic_devloop.runtime_supervisor import (
    PlannerAdmissionRepairDecisionArtifact,
    write_planner_admission_repair_decision_artifact,
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


class ManyFilesExecutor(FakeExecutor):
    def __init__(self, *, files_per_task: int) -> None:
        self._files_per_task = files_per_task

    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        result = super().run(prompt_path=prompt_path, worktree_path=worktree_path, output_dir=output_dir)
        prompt_text = prompt_path.read_text(encoding="utf-8")
        task_id = "unknown"
        if "task_id: demo-0001" in prompt_text:
            task_id = "demo-0001"
        elif "task_id: demo-0002" in prompt_text:
            task_id = "demo-0002"
        docs_dir = worktree_path / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        for index in range(1, self._files_per_task + 1):
            (docs_dir / f"{task_id}-{index}.md").write_text(f"# {task_id} {index}\n", encoding="utf-8")
        return result


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


class DependencyOrderingExecutor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started: list[str] = []
        self.task_two_started_before_task_one_finished = False
        self.task_one_finished = threading.Event()

    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        prompt_text = prompt_path.read_text(encoding="utf-8")
        task_id = "unknown"
        if "task_id: demo-0001" in prompt_text:
            task_id = "demo-0001"
        elif "task_id: demo-0002" in prompt_text:
            task_id = "demo-0002"

        with self._lock:
            self.started.append(task_id)
            if task_id == "demo-0002" and not self.task_one_finished.is_set():
                self.task_two_started_before_task_one_finished = True

        if task_id == "demo-0001":
            time.sleep(0.2)
            self.task_one_finished.set()
        else:
            self.task_one_finished.wait(timeout=2)

        output_file = worktree_path / "docs" / f"{task_id}.md"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(f"# {task_id}\n", encoding="utf-8")

        stdout_path = output_dir / "executor_stdout.log"
        stderr_path = output_dir / "executor_stderr.log"
        stdout_path.write_text("dependency ordering executor\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return ExecutorResult(
            command=["dependency-ordering-executor"],
            exit_code=0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            duration_seconds=0.01,
            backend="fake",
            model=None,
        )


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


def test_cost_runtime_governance_falls_back_without_prior_metrics(tmp_path: Path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config = ProjectConfig.model_validate(
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
            "model_roles": {},
            "model_routing": {"default_role": "worker"},
            "verification_profiles": {"default": {"commands": ["true"]}},
            "budget": {
                "max_executor_attempts_per_task": 1,
                "max_strong_model_calls_per_release": 0,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        }
    )
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    release_root = tmp_path / "current_release"
    release_root.mkdir()

    decision = _load_or_build_cost_runtime_governance_decision(
        release_root=release_root,
        release_id="v0.1.0",
        runs_dir=runs_dir,
        current_run_id="20260514T020304Z_v0.1.0_release",
        config=config,
        now=None,
        progress=None,
    )

    assert isinstance(decision, CostRuntimeGovernanceDecision)
    assert decision.selected_action == CostRuntimeGovernanceAction.DECOMPOSED
    fallback_evidence_path = release_root / "cost_runtime_governance_fallback_evidence.json"
    assert decision.evidence_paths == [fallback_evidence_path.resolve()]
    assert fallback_evidence_path.exists()
    assert isinstance(
        load_supervisor_decision_artifact(_cost_runtime_governance_decision_path(release_root, "v0.1.0")),
        CostRuntimeGovernanceDecision,
    )
    assert _cost_runtime_governance_feature_review_max_repair_loops_override(
        decision=decision,
        default_max_repair_loops=3,
    ) is None


def test_cost_runtime_governance_review_cap_writes_typed_decision_artifact(tmp_path: Path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config = ProjectConfig.model_validate(
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
            "model_roles": {},
            "model_routing": {"default_role": "worker"},
            "verification_profiles": {"default": {"commands": ["true"]}},
            "budget": {
                "max_executor_attempts_per_task": 1,
                "max_strong_model_calls_per_release": 0,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        }
    )
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    prior_release_run_dir = runs_dir / "20260514T010203Z_v0.1.0_release"
    prior_release_run_dir.mkdir(parents=True)
    (prior_release_run_dir / "release_tuning.md").write_text("# tuning\n", encoding="utf-8")
    (prior_release_run_dir / "release_metrics.json").write_text(
        json.dumps(
            {
                "run_id": "prior",
                "release_id": "v0.1.0",
                "decision": "accepted",
                "totals": {"prompt_chars": 1000, "context_chars": 1000},
                "compact_governance": {
                    "review_wave_count": 3,
                    "feature_review_repair_wave_count": 0,
                    "model_fallback_count": 0,
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    release_root = tmp_path / "current_release"
    release_root.mkdir()

    decision = _load_or_build_cost_runtime_governance_decision(
        release_root=release_root,
        release_id="v0.1.0",
        runs_dir=runs_dir,
        current_run_id="20260514T040506Z_v0.1.0_release",
        config=config,
        now=None,
        progress=None,
    )

    decision_path = _cost_runtime_governance_decision_path(release_root, "v0.1.0")
    assert decision_path.exists()
    assert isinstance(decision, CostRuntimeGovernanceDecision)
    assert decision.selected_action == CostRuntimeGovernanceAction.REVIEW_CAPPED
    assert (
        _cost_runtime_governance_feature_review_max_repair_loops_override(
            decision=decision,
            default_max_repair_loops=3,
        )
        == 1
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


@pytest.mark.parametrize("invalid_limit", [0, -1])
def test_project_config_rejects_non_positive_feature_review_convergence_limit(invalid_limit: int) -> None:
    with pytest.raises(ValidationError, match="feature_review_max_repair_loops"):
        ProjectConfig.model_validate(
            {
                "project_id": "demo",
                "repo_path": "/tmp/demo",
                "default_base_branch": "main",
                "worktree_root": "/tmp/worktrees",
                "executor": {"type": "codex_cli", "model": "fallback", "max_walltime_minutes": 5},
                "verification_profiles": {"default": {"commands": ["true"]}},
                "feature_review_max_repair_loops": invalid_limit,
                "budget": {
                    "max_executor_attempts_per_task": 2,
                    "max_strong_model_calls_per_release": 10,
                    "max_changed_files_per_task": 8,
                    "max_diff_lines_per_task": 600,
                },
            }
        )


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
            "release_finalization_policy": {"policy": "local_merge", "required_credential_env_vars": []},
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
    log = result.log_path.read_text(encoding="utf-8")
    assert "Release scheduling decision: parallel -> proceed_parallel" in log
    decision_path = supervisor_decision_artifact_path(
        release_bundle_path=tmp_path / "runs" / result.run_id,
        decision_type=SupervisorDecisionType.RELEASE_SCHEDULING,
        decision_id="v0.1.0__scheduling",
    )
    decision = load_supervisor_decision_artifact(decision_path)
    assert decision.selected_action == ReleaseSchedulingAction.PARALLEL
    assert decision.evidence_paths[0] == (tmp_path / "runs" / result.run_id / "release_overlap_report.json").resolve()
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


def test_release_dependency_map_ignores_overlap_edges_for_completed_tasks() -> None:
    active = _task_contract("demo-0002", allowed_files=["docs/guides/setup.md"]).model_copy(
        update={"depends_on": ["demo-0001"]}
    )
    completed = _task_contract("demo-0001", allowed_files=["docs/guides/**"])
    report = analyze_contract_overlaps([completed, active])

    dependencies = _release_dependency_map(
        [active],
        report,
        completed_task_ids={"demo-0001"},
    )

    assert dependencies == {}


def test_completed_release_task_ids_reads_accepted_merged_summaries(tmp_path) -> None:
    summary_dir = tmp_path / "20260512T000000Z_demo_release"
    summary_dir.mkdir(parents=True)
    bundle_1 = summary_dir / "demo-0001"
    bundle_2 = summary_dir / "demo-0002"
    bundle_3 = summary_dir / "demo-0003"
    for bundle in (bundle_1, bundle_2, bundle_3):
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "changed_files.txt").write_text("", encoding="utf-8")
    (summary_dir / "release_summary.json").write_text(
        json.dumps(
            {
                "release_id": "demo",
                "integration_branch": "feature/demo",
                "tasks": [
                    {"task_id": "demo-0001", "decision": "accepted", "merged": True, "bundle_path": str(bundle_1)},
                    {"task_id": "demo-0002", "decision": "failed", "merged": True, "bundle_path": str(bundle_2)},
                    {"task_id": "demo-0003", "decision": "accepted", "merged": False, "bundle_path": str(bundle_3)},
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

    assert completed == {"demo-0001", "demo-0003"}


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
    assert result.finalization_decision_path is not None
    assert result.finalization_decision_path.exists()
    decision = json.loads(result.finalization_decision_path.read_text(encoding="utf-8"))
    assert decision["outcome"] == "executed"
    assert decision["policy"]["policy"] == "local_merge"
    assert decision["stop_reason"] is None
    assert decision["finalization"]["merged"] is True
    assert (repo / "docs" / "demo-0001.md").exists()
    assert _git_output(repo, "branch", "--show-current").strip() == "main"


def test_run_release_finalization_stops_when_policy_missing(tmp_path: Path) -> None:
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
        merge_on_accept=True,
        release_finalize="merge-main",
    )

    assert result.finalization is None
    assert result.finalization_decision_path is not None
    decision = json.loads(result.finalization_decision_path.read_text(encoding="utf-8"))
    assert decision["outcome"] == "stopped"
    assert decision["stop_reason"] == "missing_policy"
    assert decision["git_commands"] == []
    assert not _git_object_exists(repo, "main:docs/demo-0001.md")


def test_run_release_finalization_stops_when_credentials_missing(tmp_path: Path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(
        tmp_path,
        repo,
        release_finalization_policy={"policy": "local_merge", "required_credential_env_vars": ["DEMO_CRED"]},
    )
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        _task_contract("demo-0001", allowed_files=["docs/demo-0001.md"]).model_dump(mode="json"),
    )

    with patch.dict("os.environ", {}, clear=True):
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

    assert result.finalization is None
    assert result.finalization_decision_path is not None
    decision = json.loads(result.finalization_decision_path.read_text(encoding="utf-8"))
    assert decision["outcome"] == "stopped"
    assert decision["stop_reason"] == "missing_credentials"
    assert decision["missing_credentials"] == ["DEMO_CRED"]
    assert decision["git_commands"] == []
    assert not _git_object_exists(repo, "main:docs/demo-0001.md")


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
    assert summary["final_integration_verification_path"] is not None
    assert str(result.budget_path) in review
    assert str(result.tuning_path) in review
    assert "🧾 Release Summary" in log
    assert "Release:" in log
    assert "v0.1.0" in log
    assert str(result.budget_path) in log
    assert str(result.tuning_path) in log
    assert "Good luck, future humans. 🧑‍🚀🛠️🍀" in log


def test_build_release_metrics_compact_governance_uses_zero_or_null_fallbacks_when_optional_artifacts_missing(tmp_path: Path) -> None:
    run_id = "20260514T000000Z_v0.1.0_release"
    release_id = "v0.1.0"
    runs_dir = tmp_path / "runs"
    bundle_path = runs_dir / "task_bundle"
    bundle_path.mkdir(parents=True, exist_ok=True)
    (bundle_path / "executor_attempts.json").write_text(
        json.dumps(
            [
                {
                    "model": "gpt-5.3-codex-spark",
                    "exit_code": 0,
                    "duration_seconds": 0.5,
                    "prompt_chars": 10,
                    "stdout_chars": 1,
                    "stderr_chars": 0,
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle_path / "run_state.json").write_text(
        json.dumps({"diff_lines": 1, "verification_results": [{"duration_seconds": 0.1}]}) + "\n",
        encoding="utf-8",
    )
    (bundle_path / "executor_prompt.md").write_text("prompt", encoding="utf-8")
    (bundle_path / "changed_files.txt").write_text("docs/demo-0001.md\n", encoding="utf-8")
    raw_log_path = runs_dir / run_id / "release_raw.log"
    raw_log_path.parent.mkdir(parents=True, exist_ok=True)
    raw_log_path.write_text("", encoding="utf-8")
    task_result = TaskRunResult(
        run_id="task-run-1",
        worktree_path=tmp_path / "worktree",
        bundle_path=bundle_path,
        decision=ReviewDecision(
            task_id="demo-0001",
            decision=Decision.ACCEPTED,
            reviewer=Reviewer.DETERMINISTIC,
            rationale="ok",
        ),
    )

    metrics = _build_release_metrics(
        run_id=run_id,
        release_id=release_id,
        decision=Decision.ACCEPTED,
        task_results=[task_result],
        raw_log_path=raw_log_path,
        runs_dir=runs_dir,
    )

    compact = metrics["compact_governance"]
    assert compact["model_fallback_count"] == 0
    assert compact["review_wave_count"] == 0
    assert compact["feature_review_repair_wave_count"] == 0
    assert compact["runtime_repair_attempt_count"] == 0
    assert compact["runtime_repair_success_count"] == 0
    assert compact["runtime_repair_stop_count"] == 0
    assert compact["admission_repair_count"] == 0
    assert compact["scope_risk_overage_count"] == 0
    assert compact["scope_risk_blocked_count"] == 0
    assert compact["final_review_adjudication_count"] == 0
    assert compact["final_review_continuation_outcome"] is None
    assert compact["final_review_hard_stop_reason"] is None
    assert compact["finalization_outcome"] is None
    assert compact["finalization_stop_reason"] is None
    assert compact["finalization_gate_reason"] is None


def test_build_release_metrics_compact_governance_extracts_artifact_signals_when_present(tmp_path: Path) -> None:
    run_id = "20260514T000001Z_v0.1.0_release"
    release_id = "v0.1.0"
    runs_dir = tmp_path / "runs"
    release_root = runs_dir / run_id
    bundle_path = runs_dir / "task_bundle"
    bundle_path.mkdir(parents=True, exist_ok=True)
    (bundle_path / "executor_attempts.json").write_text(
        json.dumps(
            [
                {
                    "model": "gpt-5.3-codex-spark",
                    "exit_code": 1,
                    "duration_seconds": 0.5,
                    "prompt_chars": 10,
                    "stdout_chars": 1,
                    "stderr_chars": 5,
                },
                {
                    "model": "gpt-5.3-codex",
                    "exit_code": 0,
                    "duration_seconds": 0.7,
                    "prompt_chars": 12,
                    "stdout_chars": 2,
                    "stderr_chars": 1,
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle_path / "run_state.json").write_text(
        json.dumps({"diff_lines": 12, "verification_results": [{"duration_seconds": 0.2}]}) + "\n",
        encoding="utf-8",
    )
    (bundle_path / "executor_prompt.md").write_text("prompt", encoding="utf-8")
    (bundle_path / "changed_files.txt").write_text("docs/demo-0001.md\n", encoding="utf-8")
    raw_log_path = release_root / "release_raw.log"
    raw_log_path.parent.mkdir(parents=True, exist_ok=True)
    raw_log_path.write_text("event=context_loaded task=demo-0001 chars=42\n", encoding="utf-8")
    task_result = TaskRunResult(
        run_id="task-run-1",
        worktree_path=tmp_path / "worktree",
        bundle_path=bundle_path,
        decision=ReviewDecision(
            task_id="demo-0001",
            decision=Decision.ACCEPTED,
            reviewer=Reviewer.DETERMINISTIC,
            rationale="ok",
        ),
    )
    (release_root / "feature_review" / "repairs_01").mkdir(parents=True, exist_ok=True)
    (release_root / "feature_review" / "repairs_02").mkdir(parents=True, exist_ok=True)
    (release_root / "feature_review.json").write_text("{}", encoding="utf-8")
    (release_root / "runtime_supervisor").mkdir(parents=True, exist_ok=True)
    (release_root / "runtime_supervisor" / "repair_demo-0001.json").write_text(
        json.dumps(
            {
                "attempts": [{"decision": "retry"}, {"decision": "stop"}],
                "final_result": {"decision": "accepted"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (release_root / "runtime_supervisor" / "planner_admission_repairs.json").write_text(
        json.dumps({"records": [{"task_id": "demo-0001"}, {"task_id": "demo-0002"}]}) + "\n",
        encoding="utf-8",
    )
    (release_root / "supervisor_decisions").mkdir(parents=True, exist_ok=True)
    (release_root / "supervisor_decisions" / "scope_risk_budget_policy__a.json").write_text(
        json.dumps(
            {
                "configured_changed_files_limit": 2,
                "actual_changed_files": 3,
                "configured_diff_size_limit": 50,
                "actual_diff_size": 40,
                "outcome": "stopped",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (release_root / "final_review_continuation_decision.json").write_text(
        json.dumps(
            {
                "outcome": "accepted_risk",
                "hard_stop_reason": "none",
                "finding_adjudication_paths": ["a.json", "b.json"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (release_root / "finalization_decision.json").write_text(
        json.dumps({"outcome": "stopped", "stop_reason": "failed_gate", "blocked_reason": "unresolved_required_findings"})
        + "\n",
        encoding="utf-8",
    )

    metrics = _build_release_metrics(
        run_id=run_id,
        release_id=release_id,
        decision=Decision.ACCEPTED,
        task_results=[task_result],
        raw_log_path=raw_log_path,
        runs_dir=runs_dir,
    )

    compact = metrics["compact_governance"]
    assert compact["model_fallback_count"] == 1
    assert compact["review_wave_count"] == 3
    assert compact["feature_review_repair_wave_count"] == 2
    assert compact["runtime_repair_attempt_count"] == 2
    assert compact["runtime_repair_success_count"] == 1
    assert compact["runtime_repair_stop_count"] == 1
    assert compact["admission_repair_count"] == 2
    assert compact["scope_risk_overage_count"] == 1
    assert compact["scope_risk_blocked_count"] == 1
    assert compact["final_review_adjudication_count"] == 2
    assert compact["final_review_continuation_outcome"] == "accepted_risk"
    assert compact["final_review_hard_stop_reason"] == "none"
    assert compact["finalization_outcome"] == "stopped"
    assert compact["finalization_stop_reason"] == "failed_gate"
    assert compact["finalization_gate_reason"] == "unresolved_required_findings"


def test_run_release_writes_final_integration_verification_evidence(tmp_path) -> None:
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

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    final_verification_path = Path(summary["final_integration_verification_path"])
    assert final_verification_path.exists()
    evidence = json.loads(final_verification_path.read_text(encoding="utf-8"))
    assert evidence["release_id"] == "v0.1.0"
    assert evidence["integration_branch"] == "feature/v0.1.0"
    assert evidence["integration_commit"] == _git_output(repo, "rev-parse", "feature/v0.1.0").strip()
    assert evidence["success"] is True
    assert len(evidence["command_results"]) == 1
    command_result = evidence["command_results"][0]
    assert command_result["command"] == "test -d docs"
    assert Path(command_result["stdout_path"]).exists()
    assert Path(command_result["stderr_path"]).exists()
    assert Path(evidence["verification_log_path"]).exists()
    assert Path(evidence["worktree_log_path"]).exists()
    summary_verification = summary["final_integration_verification"]
    assert summary_verification["integration_branch"] == "feature/v0.1.0"
    assert summary_verification["integration_commit"] == evidence["integration_commit"]
    assert Path(summary_verification["verification_log_path"]).exists()
    assert Path(summary_verification["worktree_log_path"]).exists()
    assert len(summary_verification["command_results"]) == 1
    assert Path(summary_verification["command_results"][0]["stdout_path"]).exists()
    assert Path(summary_verification["command_results"][0]["stderr_path"]).exists()


def test_final_integration_verification_worktree_guard_requires_exact_child(tmp_path: Path) -> None:
    output_dir = tmp_path / "release" / "final_integration_verification"
    output_dir.mkdir(parents=True)

    _assert_safe_final_integration_verification_worktree(output_dir / "worktree", output_dir)

    with pytest.raises(ValueError, match="final integration verification worktree"):
        _assert_safe_final_integration_verification_worktree(output_dir / "other", output_dir)

    with pytest.raises(ValueError, match="final integration verification worktree"):
        _assert_safe_final_integration_verification_worktree(tmp_path / "worktree", output_dir)


def test_run_release_runs_final_integration_verification_without_reviewer_role(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo)
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        _task_contract("demo-0001", allowed_files=["docs/demo-0001.md"]).model_dump(mode="json"),
    )

    with patch("agentic_devloop.release.invoke_feature_reviewer") as reviewer:
        result = run_release(
            project_id="demo",
            release_id="v0.1.0",
            config_dir=config_dir,
            contracts_dir=contracts_dir,
            runs_dir=tmp_path / "runs",
            executor=FakeExecutor(),
            merge_on_accept=True,
        )

    reviewer.assert_not_called()
    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    final_verification_path = Path(summary["final_integration_verification_path"])
    evidence = json.loads(final_verification_path.read_text(encoding="utf-8"))
    assert evidence["success"] is True


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


def test_run_release_persists_planner_admission_repairs_and_logs_attempts(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo)
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        _task_contract("demo-0001", allowed_files=["docs/demo-0001.md"]).model_dump(mode="json"),
    )
    planning_bundle = tmp_path / "runs" / "20260513T000000Z_v0.1.0_plan"
    evidence_path = planning_bundle / "planner_stdout.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text("{}", encoding="utf-8")
    decision = PlannerAdmissionRepairDecisionArtifact.model_validate(
        {
            "decision_id": "v0.1.0__demo-0001__admission_repair",
            "release_id": "v0.1.0",
            "decided_at": datetime.now(UTC),
            "decided_by": "runtime_supervisor",
            "rationale": "Broad mechanical update accepted with bounded guardrails.",
            "validators_to_rerun": ["ContractPlan", "TaskContract"],
            "evidence_paths": [evidence_path.resolve()],
            "applied": True,
            "action_payload": {
                "admission_failure_inputs": [
                    {
                        "release_id": "v0.1.0",
                        "task_id": "demo-0001",
                        "validation_errors": ["allowed_files count exceeds project budget"],
                        "policy_constraints": ["Budget limits remain hard-gated."],
                        "validators_to_rerun": ["ContractPlan", "TaskContract"],
                    }
                ],
                "selected_action": "accept_broad_but_mechanical",
                "outcome": "accept_with_mechanical_guards",
                "rationale": "Broad mechanical update accepted with bounded guardrails.",
                "fallback_plan": "Split if rerun validators fail.",
                "validators_to_rerun": ["ContractPlan", "TaskContract"],
                "evidence_paths": [evidence_path.resolve()],
                "accepted_scope_notes": ["Mechanical-only edits."],
            },
        }
    )
    decision_path = write_planner_admission_repair_decision_artifact(
        release_bundle_path=planning_bundle,
        decision=decision,
    )

    result = run_release(
        project_id="demo",
        release_id="v0.1.0",
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=tmp_path / "runs",
        executor=FakeExecutor(),
        merge_on_accept=True,
        planning_warnings=[f"supervisor_admission_repair_decision_path={decision_path}"],
    )

    assert result.decision == Decision.ACCEPTED
    artifact_path = tmp_path / "runs" / result.run_id / "runtime_supervisor" / "planner_admission_repairs.json"
    assert artifact_path.exists()
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["release_id"] == "v0.1.0"
    assert len(artifact["records"]) == 1
    record = artifact["records"][0]
    assert record["task_id"] == "demo-0001"
    assert record["selected_action"] == "accept_broad_but_mechanical"
    assert record["outcome"] == "accept_with_mechanical_guards"
    assert record["validator_rerun_succeeded"] is True
    assert "Admission repair attempt 1: task=demo-0001 action=accept_broad_but_mechanical outcome=accept_with_mechanical_guards" in (
        result.log_path.read_text(encoding="utf-8")
    )


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


def _write_scope_risk_budget_policy_decision(
    *,
    release_root: Path,
    release_id: str,
    decided_at: datetime,
    affected_task_id: str,
    selected_action: ScopeRiskAction,
    outcome: ScopeRiskOutcome,
    classification: ScopeRiskClassification = ScopeRiskClassification.MECHANICAL,
    hard_safety_findings: list[str] | None = None,
) -> Path:
    evidence_path = release_root / "scope_risk_evidence.txt"
    evidence_path.write_text("scope risk evidence\n", encoding="utf-8")
    decision = ScopeRiskBudgetPolicyDecision.model_validate(
        {
            "decision_type": SupervisorDecisionType.SCOPE_RISK_BUDGET_POLICY,
            "decision_id": f"{release_id}__scope_risk__{affected_task_id}",
            "release_id": release_id,
            "decided_at": decided_at,
            "decided_by": "test",
            "rationale": "scope-risk budget policy test decision",
            "evidence_paths": [str(Path("scope_risk_evidence.txt"))],
            "classification": classification,
            "selected_action": selected_action,
            "outcome": outcome,
            "fallback_plan": "Split or replan if validation fails.",
            "validators_to_rerun": ["verification", "release_summary"],
            "configured_changed_files_limit": 8,
            "actual_changed_files": 9,
            "configured_diff_size_limit": 600,
            "actual_diff_size": 9,
            "affected_scope": ScopeRiskAffectedScope.TASK,
            "affected_task_id": affected_task_id,
            "hard_safety_findings": hard_safety_findings or [],
        }
    )
    return write_supervisor_decision_artifact(release_bundle_path=release_root, decision=decision)


def test_run_release_generates_scope_risk_decision_and_blocks_without_explicit_acceptance(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo, max_strong_model_calls_per_release=10)
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        _task_contract("demo-0001", allowed_files=["docs/*"]).model_dump(mode="json"),
    )

    result = run_release(
        project_id="demo",
        release_id="v0.1.0",
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=tmp_path / "runs",
        executor=ManyFilesExecutor(files_per_task=9),
        merge_on_accept=True,
        release_finalize="merge-main",
    )

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert result.decision == Decision.NEEDS_REVISION
    assert result.finalization is None
    assert summary["scope_risk_budget_policy_gate"]["allowed"] is False
    assert any(
        "requires replan_and_retry" in reason
        for reason in summary["scope_risk_budget_policy_gate"]["blocking_reasons"]
    )
    decision_paths = summary["scope_risk_budget_policy_decision_paths"]
    assert decision_paths
    decision_path = Path(decision_paths[0])
    decision = load_supervisor_decision_artifact(decision_path)
    assert isinstance(decision, ScopeRiskBudgetPolicyDecision)
    assert decision.selected_action == ScopeRiskAction.REPLAN
    assert decision.outcome == ScopeRiskOutcome.REPLAN_AND_RETRY
    assert decision.classification == ScopeRiskClassification.MECHANICAL
    assert decision.evidence_paths
    assert all(not path.is_absolute() and ".." not in path.parts for path in decision.evidence_paths)
    assert any("changed_files.txt" in str(path) for path in decision.evidence_paths)
    assert decision.fallback_plan
    assert decision.validators_to_rerun


def test_run_release_scope_risk_decision_uses_structured_metrics_when_risk_parse_fails(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo, max_strong_model_calls_per_release=10)
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        _task_contract("demo-0001", allowed_files=["docs/*"]).model_dump(mode="json"),
    )

    with patch("agentic_devloop.release._parse_scope_risk_budget_from_finding", return_value=None):
        result = run_release(
            project_id="demo",
            release_id="v0.1.0",
            config_dir=config_dir,
            contracts_dir=contracts_dir,
            runs_dir=tmp_path / "runs",
            executor=ManyFilesExecutor(files_per_task=9),
            merge_on_accept=True,
            release_finalize="merge-main",
        )

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    decision = load_supervisor_decision_artifact(Path(summary["scope_risk_budget_policy_decision_paths"][0]))

    assert isinstance(decision, ScopeRiskBudgetPolicyDecision)
    assert decision.classification == ScopeRiskClassification.MECHANICAL
    assert decision.configured_changed_files_limit == 8
    assert decision.configured_diff_size_limit == 600
    assert decision.actual_changed_files > decision.configured_changed_files_limit
    assert decision.actual_diff_size >= 0


def test_run_release_allows_soft_scope_overage_with_accepted_scope_risk_decision(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo, max_strong_model_calls_per_release=10)
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        _task_contract("demo-0001", allowed_files=["docs/*"]).model_dump(mode="json"),
    )
    runs_dir = tmp_path / "runs"
    now = datetime(2026, 5, 12, tzinfo=UTC)
    run_id = make_release_run_id("v0.1.0", now)
    release_root = runs_dir / run_id
    release_root.mkdir(parents=True, exist_ok=True)
    decision_path = _write_scope_risk_budget_policy_decision(
        release_root=release_root,
        release_id="v0.1.0",
        decided_at=now,
        affected_task_id="demo-0001",
        selected_action=ScopeRiskAction.ACCEPT_WITH_GUARDS,
        outcome=ScopeRiskOutcome.ACCEPTED_WITH_GUARDS,
    )

    result = run_release(
        project_id="demo",
        release_id="v0.1.0",
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=runs_dir,
        executor=ManyFilesExecutor(files_per_task=9),
        merge_on_accept=True,
        release_finalize="merge-main",
        now=now,
    )

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert result.decision == Decision.ACCEPTED
    assert result.finalization is not None
    assert decision_path.exists()
    assert str(decision_path) in summary["scope_risk_budget_policy_gate"]["selected_decision_paths"]


def test_run_release_does_not_finalize_when_scope_risk_decision_requires_stop(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo, max_strong_model_calls_per_release=10)
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        _task_contract("demo-0001", allowed_files=["docs/*"]).model_dump(mode="json"),
    )
    runs_dir = tmp_path / "runs"
    now = datetime(2026, 5, 12, tzinfo=UTC)
    run_id = make_release_run_id("v0.1.0", now)
    release_root = runs_dir / run_id
    release_root.mkdir(parents=True, exist_ok=True)
    _write_scope_risk_budget_policy_decision(
        release_root=release_root,
        release_id="v0.1.0",
        decided_at=now,
        affected_task_id="demo-0001",
        selected_action=ScopeRiskAction.STOP,
        outcome=ScopeRiskOutcome.STOPPED,
        classification=ScopeRiskClassification.RISKY,
        hard_safety_findings=["unsafe scope escalation"],
    )

    result = run_release(
        project_id="demo",
        release_id="v0.1.0",
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=runs_dir,
        executor=ManyFilesExecutor(files_per_task=9),
        merge_on_accept=True,
        release_finalize="merge-main",
        now=now,
    )

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert result.decision == Decision.NEEDS_REVISION
    assert result.finalization is None
    assert summary["scope_risk_budget_policy_gate"]["allowed"] is False
    assert any("requires stopped" in reason for reason in summary["scope_risk_budget_policy_gate"]["blocking_reasons"])


@pytest.mark.parametrize(
    ("selected_action", "outcome", "expected_gate_phrase"),
    [
        (ScopeRiskAction.SPLIT_TASK, ScopeRiskOutcome.SPLIT_AND_RETRY, "requires split_and_retry"),
        (ScopeRiskAction.REPLAN, ScopeRiskOutcome.REPLAN_AND_RETRY, "requires replan_and_retry"),
    ],
)
def test_run_release_scope_risk_split_or_replan_decision_blocks_finalize_and_preserves_hard_gate(
    tmp_path,
    selected_action: ScopeRiskAction,
    outcome: ScopeRiskOutcome,
    expected_gate_phrase: str,
) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo, max_strong_model_calls_per_release=10)
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        _task_contract("demo-0001", allowed_files=["docs/*"]).model_dump(mode="json"),
    )
    runs_dir = tmp_path / "runs"
    now = datetime(2026, 5, 12, tzinfo=UTC)
    run_id = make_release_run_id("v0.1.0", now)
    release_root = runs_dir / run_id
    release_root.mkdir(parents=True, exist_ok=True)
    decision_path = _write_scope_risk_budget_policy_decision(
        release_root=release_root,
        release_id="v0.1.0",
        decided_at=now,
        affected_task_id="demo-0001",
        selected_action=selected_action,
        outcome=outcome,
        classification=ScopeRiskClassification.COHESIVE,
    )

    result = run_release(
        project_id="demo",
        release_id="v0.1.0",
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=runs_dir,
        executor=ManyFilesExecutor(files_per_task=9),
        merge_on_accept=True,
        release_finalize="merge-main",
        now=now,
    )

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert result.decision == Decision.NEEDS_REVISION
    assert result.finalization is None
    assert summary["scope_risk_budget_policy_gate"]["allowed"] is False
    assert any(
        expected_gate_phrase in reason for reason in summary["scope_risk_budget_policy_gate"]["blocking_reasons"]
    )
    assert str(decision_path) in summary["scope_risk_budget_policy_gate"]["selected_decision_paths"]
    assert str(decision_path) in summary["scope_risk_budget_policy_gate"]["blocking_decision_paths"]


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
    assert "Overlap-risk report: 1 finding(s)" in log
    assert "Release scheduling decision: sequential -> proceed_sequential" in log
    decision_path = supervisor_decision_artifact_path(
        release_bundle_path=tmp_path / "runs" / result.run_id,
        decision_type=SupervisorDecisionType.RELEASE_SCHEDULING,
        decision_id="v0.1.0__scheduling",
    )
    decision = load_supervisor_decision_artifact(decision_path)
    assert decision.selected_action == ReleaseSchedulingAction.SEQUENTIAL
    assert decision.outcome.value == "proceed_sequential"
    assert decision.validators_to_rerun == ["overlap_report", "execution_dag", "verification"]
    assert decision.staleness_inputs.execution_mode == "parallel"


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


def test_run_release_parallel_respects_explicit_depends_on_edges(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo, verification_command="true")
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        _task_contract("demo-0001", allowed_files=["docs/demo-0001.md"], verification_commands=["true"]).model_dump(mode="json"),
    )
    _write_yaml(
        contracts_dir / "demo-0002.yaml",
        _task_contract(
            "demo-0002",
            allowed_files=["docs/demo-0002.md"],
            verification_commands=["true"],
        )
        .model_copy(update={"depends_on": ["demo-0001"]})
        .model_dump(mode="json"),
    )

    executor = DependencyOrderingExecutor()
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
    assert executor.started == ["demo-0001", "demo-0002"]
    assert executor.task_two_started_before_task_one_finished is False
    assert "Release scheduling decision: parallel -> proceed_parallel" in result.log_path.read_text(encoding="utf-8")


def test_run_release_parallel_reports_blocked_scheduler_state_for_dependency_cycle(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo, verification_command="true")
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        _task_contract("demo-0001", allowed_files=["docs/demo-0001.md"], verification_commands=["true"])
        .model_copy(update={"depends_on": ["demo-0002"]})
        .model_dump(mode="json"),
    )
    _write_yaml(
        contracts_dir / "demo-0002.yaml",
        _task_contract("demo-0002", allowed_files=["docs/demo-0002.md"], verification_commands=["true"])
        .model_copy(update={"depends_on": ["demo-0001"]})
        .model_dump(mode="json"),
    )

    with pytest.raises(ValueError, match="unsatisfied dependencies"):
        run_release(
            project_id="demo",
            release_id="v0.1.0",
            config_dir=config_dir,
            contracts_dir=contracts_dir,
            runs_dir=tmp_path / "runs",
            executor=FakeExecutor(),
            execution_mode="parallel",
        )


@pytest.mark.parametrize("artifact_kind", ["stale", "unsupported_action"])
def test_run_release_rejects_stale_or_invalid_release_scheduling_decisions(
    tmp_path, artifact_kind: str
) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo, verification_command="true")
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    contract_paths = [
        contracts_dir / "demo-0001.yaml",
        contracts_dir / "demo-0002.yaml",
    ]
    _write_yaml(
        contract_paths[0],
        _task_contract("demo-0001", allowed_files=["docs/demo-0001.md"], verification_commands=["true"]).model_dump(mode="json"),
    )
    _write_yaml(
        contract_paths[1],
        _task_contract("demo-0002", allowed_files=["docs/demo-0002.md"], verification_commands=["true"]).model_dump(mode="json"),
    )

    overlap_report_sha256 = sha256(
        json.dumps({"findings": []}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    release_inputs_sha256 = sha256(
        json.dumps(
            {
                "release_id": "v0.1.0",
                "execution_mode": "parallel",
                "selected_task_ids": ["demo-0001", "demo-0002"],
                "selected_contract_paths": [str(path.resolve()) for path in contract_paths],
                "overlap_report_sha256": overlap_report_sha256,
                "base_branch_head_commit": _git_output(repo, "rev-parse", "HEAD").strip(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    run_id = "20260513T080000Z_v0.1.0_release"
    release_root = tmp_path / "runs" / run_id
    decision_path = supervisor_decision_artifact_path(
        release_bundle_path=release_root,
        decision_type=SupervisorDecisionType.RELEASE_SCHEDULING,
        decision_id="v0.1.0__scheduling",
    )
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "decision_id": "v0.1.0__scheduling",
        "release_id": "v0.1.0",
        "decided_at": "2026-05-13T08:00:00",
        "decided_by": "supervisor-agent",
        "rationale": "serialized source overlap",
        "evidence_paths": [str(path.resolve()) for path in contract_paths],
        "decision_type": "release_scheduling",
        "risk_level": "moderate",
        "overlap_findings": [],
        "selected_action": "stacked" if artifact_kind == "unsupported_action" else "sequential",
        "outcome": "stacked_branches" if artifact_kind == "unsupported_action" else "proceed_sequential",
        "fallback_plan": "Rerun overlap analysis before reusing the decision.",
        "validators_to_rerun": ["overlap_report", "verification"],
        "staleness_inputs": {
            "execution_mode": "parallel",
            "selected_task_ids": ["demo-0001", "demo-0002"],
            "selected_contract_paths": [str(path.resolve()) for path in contract_paths],
            "overlap_report_sha256": overlap_report_sha256,
            "base_branch_head_commit": _git_output(repo, "rev-parse", "HEAD").strip(),
            "release_inputs_sha256": "stale" if artifact_kind == "stale" else release_inputs_sha256,
        },
    }
    if artifact_kind == "unsupported_action":
        payload["fallback_plan"] = "Stacked branches are not implemented."
        payload["validators_to_rerun"] = ["overlap_report", "verification"]
    decision_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="stale|unsupported release scheduling action"):
        run_release(
            project_id="demo",
            release_id="v0.1.0",
            config_dir=config_dir,
            contracts_dir=contracts_dir,
            runs_dir=tmp_path / "runs",
            executor=FakeExecutor(),
            execution_mode="parallel",
            now=datetime(2026, 5, 13, 8, 0, 0),
        )


def test_run_release_normalizes_repairable_release_scheduling_wrapper_and_persists_decision(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo, verification_command="true")
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    contract_paths = [
        contracts_dir / "demo-0001.yaml",
        contracts_dir / "demo-0002.yaml",
    ]
    _write_yaml(
        contract_paths[0],
        _task_contract("demo-0001", allowed_files=["docs/demo-0001.md"], verification_commands=["true"]).model_dump(mode="json"),
    )
    _write_yaml(
        contract_paths[1],
        _task_contract("demo-0002", allowed_files=["docs/demo-0002.md"], verification_commands=["true"]).model_dump(mode="json"),
    )

    run_id = "20260513T080000Z_v0.1.0_release"
    release_root = tmp_path / "runs" / run_id
    decision_path = supervisor_decision_artifact_path(
        release_bundle_path=release_root,
        decision_type=SupervisorDecisionType.RELEASE_SCHEDULING,
        decision_id="v0.1.0__scheduling",
    )
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    decision_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "decision_type": "release_scheduling",
                "decision": {
                    "release_id": "v0.1.0",
                    "selected_action": "parallel",
                    "outcome": "proceed_parallel",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_release(
        project_id="demo",
        release_id="v0.1.0",
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=tmp_path / "runs",
        executor=FakeExecutor(),
        execution_mode="parallel",
        now=datetime(2026, 5, 13, 8, 0, 0),
    )

    assert result.decision == Decision.ACCEPTED
    normalized = load_supervisor_decision_artifact(decision_path)
    assert normalized.selected_action == ReleaseSchedulingAction.PARALLEL
    normalization_decision_path = supervisor_decision_artifact_path(
        release_bundle_path=release_root,
        decision_type=SupervisorDecisionType.MODEL_OUTPUT_NORMALIZATION,
        decision_id="v0.1.0__release_scheduling_output",
    )
    normalization_decision = load_supervisor_decision_artifact(normalization_decision_path)
    assert normalization_decision.selected_action.value == "apply_normalization"
    assert normalization_decision.normalized_artifact_path == decision_path.resolve()
    assert "ReleaseSchedulingDecision" in normalization_decision.validators_to_rerun


def test_run_release_scheduling_normalization_refuses_changed_selected_action_semantics(tmp_path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo, verification_command="true")
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    contract_paths = [
        contracts_dir / "demo-0001.yaml",
        contracts_dir / "demo-0002.yaml",
    ]
    _write_yaml(
        contract_paths[0],
        _task_contract("demo-0001", allowed_files=["docs/demo-0001.md"], verification_commands=["true"]).model_dump(mode="json"),
    )
    _write_yaml(
        contract_paths[1],
        _task_contract("demo-0002", allowed_files=["docs/demo-0002.md"], verification_commands=["true"]).model_dump(mode="json"),
    )

    run_id = "20260513T080000Z_v0.1.0_release"
    release_root = tmp_path / "runs" / run_id
    decision_path = supervisor_decision_artifact_path(
        release_bundle_path=release_root,
        decision_type=SupervisorDecisionType.RELEASE_SCHEDULING,
        decision_id="v0.1.0__scheduling",
    )
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    decision_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "decision_type": "release_scheduling",
                "selected_action": "parallel",
                "decision": {
                    "release_id": "v0.1.0",
                    "selected_action": "sequential",
                    "outcome": "proceed_sequential",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        run_release(
            project_id="demo",
            release_id="v0.1.0",
            config_dir=config_dir,
            contracts_dir=contracts_dir,
            runs_dir=tmp_path / "runs",
            executor=FakeExecutor(),
            execution_mode="parallel",
            now=datetime(2026, 5, 13, 8, 0, 0),
        )
    normalization_decision_path = supervisor_decision_artifact_path(
        release_bundle_path=release_root,
        decision_type=SupervisorDecisionType.MODEL_OUTPUT_NORMALIZATION,
        decision_id="v0.1.0__release_scheduling_output",
    )
    normalization_decision = load_supervisor_decision_artifact(normalization_decision_path)
    assert normalization_decision.selected_action.value == "refuse"
    assert "disagree on selected action semantics" in (normalization_decision.refusal_reason or "")


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


def test_analyze_contract_overlaps_blocks_destructive_scripts() -> None:
    destructive_report = analyze_contract_overlaps(
        [
            _task_contract("demo-0001", allowed_files=["scripts/destroy-db.sh"]),
            _task_contract("demo-0002", allowed_files=["scripts/destroy-db.sh"]),
        ]
    )

    assert destructive_report.has_blocking_findings is True


def test_analyze_contract_overlaps_blocks_generated_artifacts() -> None:
    generated_artifact_report = analyze_contract_overlaps(
        [
            _task_contract("demo-0001", allowed_files=["dist/app.min.js"]),
            _task_contract("demo-0002", allowed_files=["dist/app.min.js"]),
        ]
    )

    assert generated_artifact_report.has_blocking_findings is True


def test_runtime_supervisor_classifies_model_quota_as_missing_credentials_hard_stop(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    bundle_path.mkdir(parents=True)
    (bundle_path / "failure_diagnosis.yaml").write_text("category: model_quota\n", encoding="utf-8")
    result = TaskRunResult(
        run_id="20260513T000000Z_v0.1.0_demo-0001",
        worktree_path=tmp_path / "worktree",
        bundle_path=bundle_path,
        decision=ReviewDecision(
            task_id="demo-0001",
            decision=Decision.FAILED,
            reviewer=Reviewer.DETERMINISTIC,
            rationale="quota exceeded",
        ),
    )

    classification, event_kind, category = _runtime_supervisor_classification_for_task_result(
        result=result,
        task=_task_contract("demo-0001"),
    )

    assert classification == "missing_credentials"
    assert str(event_kind) == "release_blocked"
    assert category == "model_quota"


def test_runtime_supervisor_stops_when_environment_repair_decision_is_stop(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    decision_dir = bundle_path / "supervisor_decisions"
    decision_dir.mkdir(parents=True)
    (bundle_path / "failure_diagnosis.yaml").write_text("category: verification_failure\n", encoding="utf-8")
    (bundle_path / "verification.log").write_text("verification failed\n", encoding="utf-8")
    (bundle_path / "changed_files.txt").write_text("", encoding="utf-8")
    write_supervisor_decision_artifact(
        release_bundle_path=bundle_path,
        decision=EnvironmentRepairDecision.model_validate(
            {
                "decision_id": "demo-0001__verification_environment_repair",
                "release_id": "demo",
                "decided_at": datetime(2026, 5, 13, 0, 0, tzinfo=UTC),
                "decided_by": "deterministic_kernel",
                "rationale": "missing policy",
                "evidence_paths": ["verification.log", "changed_files.txt"],
                "policy_basis": "deterministic_verification_environment_repair_policy_v1",
                "selected_policy_action": EnvironmentRepairPolicyAction.STOP,
                "outcome": "stop",
                "fallback_plan": "Stop and request operator intervention.",
                "source_evidence_paths": ["verification.log"],
                "retry_budget_impact": "stop_release_retry_budget",
                "validators_to_rerun": ["false"],
                "refusal_reason": "missing policy configuration",
                "capture_commands": [],
            }
        ),
    )

    result = TaskRunResult(
        run_id="20260513T000000Z_demo_demo-0001",
        worktree_path=tmp_path / "worktree",
        bundle_path=bundle_path,
        decision=ReviewDecision(
            task_id="demo-0001",
            decision=Decision.FAILED,
            reviewer=Reviewer.DETERMINISTIC,
            rationale="verification failed",
        ),
    )

    classification, event_kind, category = _runtime_supervisor_classification_for_task_result(
        result=result,
        task=_task_contract("demo-0001"),
    )

    assert classification == "unsafe_policy_expansion"
    assert str(event_kind) == "release_blocked"
    assert category == "verification_failure"


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

    objective_path = tmp_path / "objective.yaml"
    _write_yaml(
        objective_path,
        {
            "release_id": "demo",
            "title": "Demo release",
            "objective": "Ship one bounded increment.",
            "acceptance_criteria": ["Contract evidence exists."],
        },
    )

    artifact_path = collect_release_planning_state_review_snapshot(
        config_repo_path=repo,
        repo_state_path=Path("repo_state/demo"),
        runs_dir=runs_dir,
        planning_artifacts_dir=artifacts_dir,
        objective_path=objective_path,
        context_bundle_max_chars=10_000,
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

    manifest_path = artifacts_dir / "state_review_context_manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["phase"] == "state_review"
    assert manifest["release_id"] == "demo"
    assert manifest["included_categories"][0] == "objective"
    assert "repo_state_memory" in manifest["included_categories"]
    assert manifest["state_review_snapshot_path"] == str((artifacts_dir / "state_review_snapshot.json").resolve())


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

    manifest_path = artifacts_dir / "state_review_context_manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["phase"] == "state_review"
    assert "objective" in manifest["omitted_categories"]


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


def test_state_review_context_bundle_manifest_records_truncation_when_budget_hit(tmp_path: Path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    artifacts_dir = tmp_path / "planning_artifacts"
    artifacts_dir.mkdir(parents=True)
    objective_path = tmp_path / "objective.yaml"
    _write_yaml(
        objective_path,
        {
            "release_id": "demo",
            "title": "Huge objective",
            "objective": "X" * 50_000,
            "acceptance_criteria": ["Contract evidence exists."],
        },
    )

    collect_release_planning_state_review_snapshot(
        config_repo_path=repo,
        repo_state_path=None,
        runs_dir=tmp_path / "runs",
        planning_artifacts_dir=artifacts_dir,
        objective_path=objective_path,
        context_bundle_max_chars=1_000,
        now=datetime(2026, 5, 13, 0, 0, tzinfo=UTC),
    )

    manifest = json.loads((artifacts_dir / "state_review_context_manifest.json").read_text(encoding="utf-8"))
    assert manifest["max_chars"] == 1_000
    assert manifest["truncation_records"]
    assert "repo_state_memory" in manifest["omitted_categories"]


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
            "release_finalization_policy": {"policy": "local_merge", "required_credential_env_vars": []},
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
    assert summary["feature_review_bundle_manifest_paths"]
    manifest_path = Path(summary["feature_review_bundle_manifest_paths"][0])
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "included_categories" in manifest
    assert "omitted_categories" in manifest
    assert "size_metrics" in manifest
    assert "truncation_records" in manifest
    assert "artifact_paths" in manifest
    recheck = json.loads(Path(summary["feature_review_recheck_path"]).read_text(encoding="utf-8"))
    assert recheck["stop_reason"] == "resolved"

    classification_path = supervisor_decision_artifact_path(
        release_bundle_path=result.summary_path.parent,
        decision_type=SupervisorDecisionType.FEATURE_REVIEW_FINDING_CLASSIFICATION,
        decision_id="v0.1.0__feature_review_finding__finding-1",
    )
    assert classification_path.exists()
    loaded = load_supervisor_decision_artifact(classification_path)
    assert isinstance(loaded, FeatureReviewFindingClassificationDecision)
    assert loaded.finding_id == "finding-1"
    assert loaded.classification.value == "blocker"
    assert loaded.selected_action.value == "repair"


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
            "release_finalization_policy": {"policy": "local_merge", "required_credential_env_vars": []},
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
            "release_finalization_policy": {"policy": "local_merge", "required_credential_env_vars": []},
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


def test_run_release_feature_review_ignores_unknown_rerun_commands_and_uses_default_profile(tmp_path: Path) -> None:
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
            "release_finalization_policy": {"policy": "local_merge", "required_credential_env_vars": []},
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
                "rerun_verification_commands": [".venv/bin/python -m pytest"],
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
    assert rerun.call_args.kwargs["commands"] == ["test -d docs", "test -f README.md"]
    assert "event=feature_review_verification_commands_ignored" in result.log_path.read_text(encoding="utf-8")


def test_run_release_feature_review_normalizes_derivable_empty_evidence_paths(tmp_path: Path) -> None:
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
            "verification_profiles": {"default": {"commands": ["test -d docs"]}},
            "release_finalization_policy": {"policy": "local_merge", "required_credential_env_vars": []},
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

    blocked = FeatureReviewDecision.model_validate(
        {
            "release_id": "v0.1.0",
            "reviewer": "deterministic",
            "summary": "blocked",
            "recommendation": "escalate",
            "accepted_risks": [],
            "rerun_verification_commands": [],
            "findings": [
                {
                    "finding_id": "v0.1.0:feature_review_blocked",
                    "severity": "critical",
                    "summary": "Reviewer output was not valid FeatureReviewDecision JSON: evidence_paths invalid",
                    "affected_files": ["feature_review_context"],
                    "evidence_paths": [str(tmp_path / "dummy.log")],
                    "required_repairs": ["rerun"],
                    "optional_follow_ups": [],
                }
            ],
        }
    )

    class FakeBackendResult:
        def __init__(self, decision: FeatureReviewDecision, raw_output: str, output_dir: Path) -> None:
            self.decision = decision
            self.raw_output = raw_output
            output_dir.mkdir(parents=True, exist_ok=True)
            self.prompt_path = output_dir / "feature_review_prompt.md"
            self.stdout_path = output_dir / "feature_review_stdout.log"
            self.stderr_path = output_dir / "feature_review_stderr.log"
            self.metadata_path = output_dir / "feature_review_metadata.json"
            self.prompt_path.write_text("prompt\n", encoding="utf-8")
            self.stdout_path.write_text(raw_output, encoding="utf-8")
            self.stderr_path.write_text("", encoding="utf-8")
            self.metadata_path.write_text('{"ok":true}\n', encoding="utf-8")

    raw_output = json.dumps(
        {
            "decision": {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Optional follow-up only.",
                "recommendation": "approve",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "opt-1",
                        "severity": "low",
                        "summary": "Optional clarity update.",
                        "affected_files": ["docs/demo-0001.md"],
                        "evidence_paths": [],
                        "required_repairs": [],
                        "optional_follow_ups": ["Consider adding detail."],
                    }
                ],
            }
        }
    )

    def fake_invoke_feature_reviewer(*_args, **kwargs):
        return FakeBackendResult(blocked, raw_output, kwargs["output_dir"])

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
    normalized_decision = json.loads(Path(summary["feature_review_path"]).read_text(encoding="utf-8"))
    normalization_decision_path = Path(summary["feature_review_output_normalization_decision_path"])
    normalized_artifact_path = Path(summary["feature_review_normalized_artifact_path"])
    assert result.decision == Decision.ACCEPTED
    assert Path(summary["feature_review_prompt_path"]).exists()
    assert Path(summary["feature_review_stdout_path"]).exists()
    assert Path(summary["feature_review_stderr_path"]).exists()
    assert Path(summary["feature_review_metadata_path"]).exists()
    assert normalization_decision_path.exists()
    assert normalized_artifact_path.exists()
    assert normalized_decision["findings"][0]["evidence_paths"] == ["docs/demo-0001.md"]

    decision_path = supervisor_decision_artifact_path(
        release_bundle_path=result.summary_path.parent,
        decision_type=SupervisorDecisionType.MODEL_OUTPUT_NORMALIZATION,
        decision_id="v0.1.0__feature_review_output",
    )
    decision = load_supervisor_decision_artifact(decision_path)
    assert decision.selected_action.value == "apply_normalization"
    assert decision.validators_to_rerun == ["FeatureReviewDecision", "ReviewDecision"]
    assert normalization_decision_path == decision_path


def test_run_release_feature_review_normalization_rejects_semantic_changes(tmp_path: Path) -> None:
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

    blocked = FeatureReviewDecision.model_validate(
        {
            "release_id": "v0.1.0",
            "reviewer": "deterministic",
            "summary": "blocked",
            "recommendation": "escalate",
            "accepted_risks": [],
            "rerun_verification_commands": [],
            "findings": [
                {
                    "finding_id": "v0.1.0:feature_review_blocked",
                    "severity": "critical",
                    "summary": "Reviewer output was not valid FeatureReviewDecision JSON: wrapper drift",
                    "affected_files": ["feature_review_context"],
                    "evidence_paths": [str(tmp_path / "dummy.log")],
                    "required_repairs": ["rerun"],
                    "optional_follow_ups": [],
                }
            ],
        }
    )

    class FakeBackendResult:
        def __init__(self, decision: FeatureReviewDecision, raw_output: str, output_dir: Path) -> None:
            self.decision = decision
            self.raw_output = raw_output
            output_dir.mkdir(parents=True, exist_ok=True)
            self.prompt_path = output_dir / "feature_review_prompt.md"
            self.stdout_path = output_dir / "feature_review_stdout.log"
            self.stderr_path = output_dir / "feature_review_stderr.log"
            self.metadata_path = output_dir / "feature_review_metadata.json"
            self.prompt_path.write_text("prompt\n", encoding="utf-8")
            self.stdout_path.write_text(raw_output, encoding="utf-8")
            self.stderr_path.write_text("", encoding="utf-8")
            self.metadata_path.write_text('{"ok":true}\n', encoding="utf-8")

    raw_output = json.dumps(
        {
            "summary": "Top-level summary one",
            "recommendation": "approve",
            "findings": [
                {"finding_id": "f-1", "severity": "low", "summary": "s1", "affected_files": ["docs/demo-0001.md"]}
            ],
            "decision": {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Nested summary two",
                "recommendation": "approve",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "f-1",
                        "severity": "high",
                        "summary": "s1",
                        "affected_files": ["docs/demo-0001.md"],
                        "evidence_paths": [],
                        "required_repairs": [],
                        "optional_follow_ups": [],
                    }
                ],
            },
        }
    )

    def fake_invoke_feature_reviewer(*_args, **kwargs):
        return FakeBackendResult(blocked, raw_output, kwargs["output_dir"])

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

    assert result.decision == Decision.ESCALATED
    decision_path = supervisor_decision_artifact_path(
        release_bundle_path=result.summary_path.parent,
        decision_type=SupervisorDecisionType.MODEL_OUTPUT_NORMALIZATION,
        decision_id="v0.1.0__feature_review_output",
    )
    decision = load_supervisor_decision_artifact(decision_path)
    assert decision.selected_action.value == "refuse"
    assert "disagree on finding semantics" in (decision.refusal_reason or "")


def test_run_release_feature_review_normalizes_truncated_context_limitation(tmp_path: Path) -> None:
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

    blocked = FeatureReviewDecision.model_validate(
        {
            "release_id": "v0.1.0",
            "reviewer": "deterministic",
            "summary": "blocked",
            "recommendation": "escalate",
            "accepted_risks": [],
            "rerun_verification_commands": [],
            "findings": [
                {
                    "finding_id": "v0.1.0:feature_review_blocked",
                    "severity": "critical",
                    "summary": "Reviewer output was not valid FeatureReviewDecision JSON: limitations only",
                    "affected_files": ["feature_review_context"],
                    "evidence_paths": [str(tmp_path / "dummy.log")],
                    "required_repairs": ["rerun"],
                    "optional_follow_ups": [],
                }
            ],
        }
    )

    class FakeBackendResult:
        def __init__(self, decision: FeatureReviewDecision, raw_output: str, output_dir: Path) -> None:
            self.decision = decision
            self.raw_output = raw_output
            output_dir.mkdir(parents=True, exist_ok=True)
            self.prompt_path = output_dir / "feature_review_prompt.md"
            self.stdout_path = output_dir / "feature_review_stdout.log"
            self.stderr_path = output_dir / "feature_review_stderr.log"
            self.metadata_path = output_dir / "feature_review_metadata.json"
            self.prompt_path.write_text("prompt\n", encoding="utf-8")
            self.stdout_path.write_text(raw_output, encoding="utf-8")
            self.stderr_path.write_text("", encoding="utf-8")
            self.metadata_path.write_text('{"ok":true}\n', encoding="utf-8")

    raw_output = json.dumps(
        {
            "decision": {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Approved with limitations noted.",
                "recommendation": "approve",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [],
                "limitations": [
                    {
                        "type": "truncated_context",
                        "summary": "Context was truncated for git diff due to token budget.",
                    }
                ],
            }
        }
    )

    def fake_invoke_feature_reviewer(*_args, **kwargs):
        return FakeBackendResult(blocked, raw_output, kwargs["output_dir"])

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
    normalized_decision = json.loads(Path(summary["feature_review_path"]).read_text(encoding="utf-8"))
    limitation = next(item for item in normalized_decision["findings"] if item["finding_id"].startswith("limitation-"))
    assert result.decision == Decision.ACCEPTED
    assert limitation["affected_files"] == ["feature_review_context"]
    assert limitation["optional_follow_ups"]
    assert limitation["evidence_paths"]


def test_run_release_feature_review_normalization_keeps_missing_required_final_verification_as_hard_stop(
    tmp_path: Path,
) -> None:
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

    blocked = FeatureReviewDecision.model_validate(
        {
            "release_id": "v0.1.0",
            "reviewer": "deterministic",
            "summary": "blocked",
            "recommendation": "escalate",
            "accepted_risks": [],
            "rerun_verification_commands": [],
            "findings": [
                {
                    "finding_id": "v0.1.0:feature_review_blocked",
                    "severity": "critical",
                    "summary": "Reviewer output was not valid FeatureReviewDecision JSON: limitations only",
                    "affected_files": ["feature_review_context"],
                    "evidence_paths": [str(tmp_path / "dummy.log")],
                    "required_repairs": ["rerun"],
                    "optional_follow_ups": [],
                }
            ],
        }
    )

    class FakeBackendResult:
        def __init__(self, decision: FeatureReviewDecision, raw_output: str, output_dir: Path) -> None:
            self.decision = decision
            self.raw_output = raw_output
            output_dir.mkdir(parents=True, exist_ok=True)
            self.prompt_path = output_dir / "feature_review_prompt.md"
            self.stdout_path = output_dir / "feature_review_stdout.log"
            self.stderr_path = output_dir / "feature_review_stderr.log"
            self.metadata_path = output_dir / "feature_review_metadata.json"
            self.prompt_path.write_text("prompt\n", encoding="utf-8")
            self.stdout_path.write_text(raw_output, encoding="utf-8")
            self.stderr_path.write_text("", encoding="utf-8")
            self.metadata_path.write_text('{"ok":true}\n', encoding="utf-8")

    raw_output = json.dumps(
        {
            "decision": {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Needs evidence handoff.",
                "recommendation": "approve",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [],
                "limitations": [
                    {
                        "type": "missing_evidence_reference",
                        "summary": "Required final integration verification evidence is missing; this is a hard stop.",
                    }
                ],
            }
        }
    )

    def fake_invoke_feature_reviewer(*_args, **kwargs):
        return FakeBackendResult(blocked, raw_output, kwargs["output_dir"])

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
    decision_payload = json.loads(Path(summary["feature_review_path"]).read_text(encoding="utf-8"))
    recheck = json.loads(Path(summary["feature_review_recheck_path"]).read_text(encoding="utf-8"))
    limitation = next(item for item in decision_payload["findings"] if item["finding_id"].startswith("limitation-"))
    assert result.decision == Decision.ESCALATED
    assert decision_payload["recommendation"] == "escalate"
    assert limitation["required_repairs"]
    assert recheck["stop_reason"] == "blocked_by_hard_gate"


def test_command_with_env_prefixes_wraps_leading_assignments() -> None:
    assert _command_with_env_prefixes(["PYTHONPATH=src", "/tmp/python", "-m", "pytest"]) == [
        "/usr/bin/env",
        "PYTHONPATH=src",
        "/tmp/python",
        "-m",
        "pytest",
    ]
    assert _command_with_env_prefixes(["git", "diff", "--check"]) == ["git", "diff", "--check"]


def test_feature_review_rerun_worktree_cleanup_guard_requires_rerun_root(tmp_path: Path) -> None:
    rerun_root = (tmp_path / "runs" / "feature_review" / "verification_rerun_01").resolve()
    safe_worktree = rerun_root / "worktree"
    unsafe_worktree = (tmp_path / "worktrees" / "not-rerun-owned").resolve()

    _assert_safe_feature_review_rerun_worktree(safe_worktree, rerun_root)

    with pytest.raises(ValueError, match="forced cleanup"):
        _assert_safe_feature_review_rerun_worktree(unsafe_worktree, rerun_root)


def test_feature_review_required_finding_adjudication_is_limited_to_verification_only_findings() -> None:
    syntax_finding = FeatureReviewDecision.model_validate(
        {
            "release_id": "demo",
            "reviewer": "hybrid",
            "summary": "review",
            "recommendation": "require_repairs",
            "accepted_risks": [],
            "rerun_verification_commands": [],
            "findings": [
                {
                    "finding_id": "syntax-risk",
                    "severity": "critical",
                    "summary": "Confirm the module parses; possible syntax issue in diff excerpt.",
                    "affected_files": ["src/agentic_devloop/feature_review.py"],
                    "required_repairs": [
                        "Open the file and ensure strings are valid; fix formatting if needed.",
                        "Rerun compileall and pytest to confirm imports pass.",
                    ],
                },
                {
                    "finding_id": "real-change",
                    "severity": "high",
                    "summary": "Missing implementation behavior.",
                    "affected_files": ["src/agentic_devloop/release.py"],
                    "required_repairs": ["Implement the missing finalization behavior."],
                },
            ],
        }
    )

    accepted = [
        finding.finding_id
        for finding in syntax_finding.findings
        if _is_verification_only_or_conditional_finding(finding)
    ]

    assert accepted == ["syntax-risk"]


def test_run_release_feature_review_optional_findings_are_accepted_not_resolved(tmp_path: Path) -> None:
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
            "summary": "Approve with optional follow-up.",
            "recommendation": "approve_with_repairs",
            "accepted_risks": [],
            "rerun_verification_commands": [],
            "findings": [
                {
                    "finding_id": "optional-1",
                    "severity": "low",
                    "summary": "Optional polish.",
                    "affected_files": ["docs/demo-0001.md"],
                    "required_repairs": [],
                    "optional_follow_ups": ["Consider a later docs cleanup."],
                }
            ],
        }
    )

    class FakeBackendResult:
        def __init__(self, decision: FeatureReviewDecision) -> None:
            self.decision = decision

    with patch("agentic_devloop.release.invoke_feature_reviewer", return_value=FakeBackendResult(decision)):
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
    recheck = json.loads(Path(summary["feature_review_recheck_path"]).read_text(encoding="utf-8"))
    assert result.decision == Decision.ACCEPTED
    assert recheck["resolved_finding_ids"] == []
    assert recheck["accepted_finding_ids"] == ["optional-1"]
    assert recheck["deferred_finding_ids"] == []
    assert recheck["stop_reason"] == "accepted_with_rationale"

    classification_path = supervisor_decision_artifact_path(
        release_bundle_path=result.summary_path.parent,
        decision_type=SupervisorDecisionType.FEATURE_REVIEW_FINDING_CLASSIFICATION,
        decision_id="v0.1.0__feature_review_finding__optional-1",
    )
    assert classification_path.exists()
    loaded = load_supervisor_decision_artifact(classification_path)
    assert isinstance(loaded, FeatureReviewFindingClassificationDecision)
    assert loaded.finding_id == "optional-1"
    assert loaded.classification.value == "soft_finding"
    assert loaded.selected_action.value == "accept"
    assert loaded.outcome.value == "continue"
    assert "matched_previous_finding_id=none" in loaded.rationale


def test_run_release_feature_review_persists_deferred_duplicate_classification_evidence(tmp_path: Path) -> None:
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
                "summary": "Required repair first.",
                "recommendation": "require_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "prior-a",
                        "severity": "high",
                        "summary": "Update docs wording for safety and consistency.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Update docs wording."],
                        "optional_follow_ups": [],
                    }
                ],
            }
        ),
        FeatureReviewDecision.model_validate(
            {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Approve with deferred duplicate follow-up.",
                "recommendation": "approve_with_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "optional-dup",
                        "severity": "low",
                        "summary": "Update docs wording for safety and consistency.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": [],
                        "optional_follow_ups": ["Consider a cleanup pass later."],
                    }
                ],
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
    recheck = json.loads(Path(summary["feature_review_recheck_path"]).read_text(encoding="utf-8"))
    log_text = result.log_path.read_text(encoding="utf-8")

    assert result.decision == Decision.ACCEPTED
    assert recheck["accepted_finding_ids"] == []
    assert recheck["deferred_finding_ids"] == ["optional-dup"]
    assert "event=feature_review_non_blocking_finding_classified" in log_text
    assert "matched_previous_finding_id=prior-a" in log_text

    classification_path = supervisor_decision_artifact_path(
        release_bundle_path=result.summary_path.parent,
        decision_type=SupervisorDecisionType.FEATURE_REVIEW_FINDING_CLASSIFICATION,
        decision_id="v0.1.0__feature_review_finding__optional-dup",
    )
    assert classification_path.exists()
    loaded = load_supervisor_decision_artifact(classification_path)
    assert isinstance(loaded, FeatureReviewFindingClassificationDecision)
    assert loaded.finding_id == "optional-dup"
    assert loaded.classification.value == "duplicate"
    assert loaded.selected_action.value == "defer"
    assert loaded.outcome.value == "stop"
    assert "matched_previous_finding_id=prior-a" in loaded.rationale
    assert "adjacent_similarity=" in loaded.rationale


def test_run_release_feature_review_prompt_includes_objective_and_prior_artifact_sections(tmp_path: Path) -> None:
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
    task = _task_contract("demo-0001", allowed_files=["docs/demo-0001.md"]).model_copy(
        update={"objective": "Document integration handoff behavior for feature review context."}
    )
    _write_yaml(
        contracts_dir / "demo-0001.yaml",
        task.model_dump(mode="json"),
    )

    captured_prompt: dict[str, str] = {}
    decision = FeatureReviewDecision.model_validate(
        {
            "release_id": "v0.1.0",
            "reviewer": "strong_model",
            "summary": "Approve.",
            "recommendation": "approve",
            "accepted_risks": [],
            "rerun_verification_commands": [],
            "findings": [],
        }
    )

    class FakeBackendResult:
        def __init__(self, decision: FeatureReviewDecision) -> None:
            self.decision = decision

    def fake_invoke_feature_reviewer(*_args, **kwargs):
        captured_prompt["text"] = kwargs["prompt"]
        return FakeBackendResult(decision)

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

    assert result.decision == Decision.ACCEPTED
    prompt = captured_prompt["text"]
    assert "Release objective: Document integration handoff behavior for feature review context." in prompt
    assert "Prior review/recheck artifacts (latest matching release run):" in prompt
    assert "final_integration_verification_log_path" in prompt


def test_run_release_feature_review_records_scope_and_backlog_follow_up_proposals(tmp_path: Path) -> None:
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
                        "finding_id": "required-1",
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
                "summary": "Repairs applied; new follow-ups are out of scope.",
                "recommendation": "approve_with_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "followup-backlog",
                        "severity": "low",
                        "summary": "Consider tightening docs wording further.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": [],
                        "optional_follow_ups": ["Track a later polish pass."],
                    },
                    {
                        "finding_id": "followup-scope",
                        "severity": "low",
                        "summary": "Consider adding a README example.",
                        "affected_files": ["README.md"],
                        "required_repairs": [],
                        "optional_follow_ups": ["Track adding a README example later."],
                    },
                ],
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
    recheck = json.loads(Path(summary["feature_review_recheck_path"]).read_text(encoding="utf-8"))
    proposals = summary["feature_review_proposals"]
    assert recheck["accepted_finding_ids"] == []
    assert recheck["deferred_finding_ids"] == ["followup-backlog", "followup-scope"]
    assert isinstance(proposals, list)
    kinds = {item["finding_id"]: item["classification"] for item in proposals}
    assert kinds["followup-backlog"] == "backlog_follow_up"
    assert kinds["followup-scope"] == "scope_expansion"

    for finding_id in ["followup-backlog", "followup-scope"]:
        classification_path = supervisor_decision_artifact_path(
            release_bundle_path=result.summary_path.parent,
            decision_type=SupervisorDecisionType.FEATURE_REVIEW_FINDING_CLASSIFICATION,
            decision_id=f"v0.1.0__feature_review_finding__{finding_id}",
        )
        loaded = load_supervisor_decision_artifact(classification_path)
        assert isinstance(loaded, FeatureReviewFindingClassificationDecision)
        assert loaded.finding_id == finding_id
        assert loaded.selected_action.value == "defer"
        assert loaded.evidence_paths


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
            "release_finalization_policy": {"policy": "local_merge", "required_credential_env_vars": []},
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
            "release_finalization_policy": {"policy": "local_merge", "required_credential_env_vars": []},
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


def test_run_release_feature_review_full_repair_recheck_finalization_regression(tmp_path: Path) -> None:
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
            "release_finalization_policy": {"policy": "local_merge", "required_credential_env_vars": []},
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
                "summary": "Repairs verified.",
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
    recheck = json.loads(Path(summary["feature_review_recheck_path"]).read_text(encoding="utf-8"))
    review = result.review_path.read_text(encoding="utf-8")

    assert result.decision == Decision.ACCEPTED
    assert result.finalization is not None
    assert result.finalization.merged is True
    assert result.finalization_gate is not None
    assert result.finalization_gate["allowed"] is True
    assert result.finalization_gate["reason"] == "allowed"
    assert result.finalization_gate["unresolved_required_finding_ids"] == []
    assert summary["feature_review_path"] is not None
    assert summary["feature_review_recheck_path"] is not None
    assert summary["finalization_gate"]["allowed"] is True
    assert summary["finalization_gate"]["reason"] == "allowed"
    assert summary["finalization_gate"]["unresolved_required_finding_ids"] == []
    assert recheck["stop_reason"] == "resolved"
    assert rerun.call_count == 1
    assert rerun.call_args.kwargs["commands"] == ["test -d docs"]
    assert not decisions
    assert "## Feature Review" in review
    assert "- Gate reason: `allowed`" in review
    assert "- Unresolved required findings: `0`" in review


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
    finalization_decision_path = Path(summary["finalization_decision_path"])
    assert finalization_decision_path.exists()
    finalization_decision = json.loads(finalization_decision_path.read_text(encoding="utf-8"))
    assert finalization_decision["outcome"] == "stopped"
    assert finalization_decision["stop_reason"] == "failed_gate"
    assert finalization_decision["git_commands"] == []
    assert "- Gate reason: `unresolved_required_findings`" in review
    assert "- Unresolved required findings: `1`" in review


def test_run_release_feature_review_required_findings_stop_at_convergence_limit(tmp_path: Path) -> None:
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
            "feature_review_max_repair_loops": 1,
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
                        "finding_id": "finding-repeat",
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
                "summary": "Still needs the same repair.",
                "recommendation": "require_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "finding-repeat",
                        "severity": "high",
                        "summary": "Fix required.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Update docs again."],
                        "optional_follow_ups": [],
                    }
                ],
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
    recheck = json.loads(Path(summary["feature_review_recheck_path"]).read_text(encoding="utf-8"))
    log_text = result.log_path.read_text(encoding="utf-8")

    assert recheck["stop_reason"] == "blocked_by_retry_budget"
    assert recheck["unresolved_finding_ids"] == ["finding-repeat"]
    assert "event=feature_review_convergence_limit_reached" in log_text
    assert "limit=1" in log_text
    assert Path(summary["final_integration_verification_path"]).exists()
    assert not decisions

    classification_path = supervisor_decision_artifact_path(
        release_bundle_path=result.summary_path.parent,
        decision_type=SupervisorDecisionType.FEATURE_REVIEW_FINDING_CLASSIFICATION,
        decision_id="v0.1.0__feature_review_finding__finding-repeat",
    )
    assert classification_path.exists()
    loaded = load_supervisor_decision_artifact(classification_path)
    assert isinstance(loaded, FeatureReviewFindingClassificationDecision)
    assert loaded.finding_id == "finding-repeat"
    assert loaded.classification.value == "blocker"

    final_path = supervisor_decision_artifact_path(
        release_bundle_path=result.summary_path.parent,
        decision_type=SupervisorDecisionType.FINAL_REVIEW_FINDING_ADJUDICATION,
        decision_id="v0.1.0__final_review_finding__finding-repeat",
    )
    assert final_path.exists()
    final_loaded = load_supervisor_decision_artifact(final_path)
    assert isinstance(final_loaded, FinalReviewFindingAdjudicationDecision)
    assert final_loaded.finding_id == "finding-repeat"
    assert final_loaded.classification.value == "blocker"


def test_run_release_feature_review_accepts_verification_only_required_finding_after_retry_budget(
    tmp_path: Path,
) -> None:
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
            "verification_profiles": {"default": {"commands": ["true"]}},
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
                "summary": "Needs a verification-only repair.",
                "recommendation": "require_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "verification-only",
                        "severity": "high",
                        "summary": "Verify syntax and rerun pytest to confirm.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Rerun pytest if needed."],
                        "optional_follow_ups": [],
                    }
                ],
            }
        ),
        FeatureReviewDecision.model_validate(
            {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Still wants verification.",
                "recommendation": "require_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "verification-only",
                        "severity": "high",
                        "summary": "Verify syntax and rerun pytest to confirm.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Rerun pytest if needed."],
                        "optional_follow_ups": [],
                    }
                ],
            }
        ),
        FeatureReviewDecision.model_validate(
            {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Retry budget reached; accept based on verification rerun.",
                "recommendation": "require_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "verification-only",
                        "severity": "high",
                        "summary": "Verify syntax and rerun pytest to confirm.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Rerun pytest if needed."],
                        "optional_follow_ups": [],
                    }
                ],
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
    recheck = json.loads(Path(summary["feature_review_recheck_path"]).read_text(encoding="utf-8"))
    assert result.decision == Decision.ACCEPTED
    assert recheck["stop_reason"] == "accepted_with_rationale"
    assert recheck["accepted_finding_ids"] == ["verification-only"]

    classification_path = supervisor_decision_artifact_path(
        release_bundle_path=result.summary_path.parent,
        decision_type=SupervisorDecisionType.FEATURE_REVIEW_FINDING_CLASSIFICATION,
        decision_id="v0.1.0__feature_review_finding__verification-only",
    )
    loaded = load_supervisor_decision_artifact(classification_path)
    assert isinstance(loaded, FeatureReviewFindingClassificationDecision)
    assert loaded.classification.value == "false_positive"
    assert loaded.selected_action.value == "accept"
    assert any("verification.log" in str(path) for path in loaded.evidence_paths)


def test_run_release_feature_review_final_adjudication_only_accepts_verification_only_required_findings(
    tmp_path: Path,
) -> None:
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
            "verification_profiles": {"default": {"commands": ["true"]}},
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
                "summary": "Two required findings remain.",
                "recommendation": "require_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "verification-only",
                        "severity": "high",
                        "summary": "Verify syntax and rerun pytest to confirm.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Rerun pytest if needed."],
                        "optional_follow_ups": [],
                    },
                    {
                        "finding_id": "real-change",
                        "severity": "high",
                        "summary": "Behavior is missing.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Implement the missing finalization behavior."],
                        "optional_follow_ups": [],
                    },
                ],
            }
        ),
        FeatureReviewDecision.model_validate(
            {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Two required findings still remain.",
                "recommendation": "require_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "verification-only",
                        "severity": "high",
                        "summary": "Verify syntax and rerun pytest to confirm.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Rerun pytest if needed."],
                        "optional_follow_ups": [],
                    },
                    {
                        "finding_id": "real-change",
                        "severity": "high",
                        "summary": "Behavior is missing.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Implement the missing finalization behavior."],
                        "optional_follow_ups": [],
                    },
                ],
            }
        ),
        FeatureReviewDecision.model_validate(
            {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Retry budget reached.",
                "recommendation": "require_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "verification-only",
                        "severity": "high",
                        "summary": "Verify syntax and rerun pytest to confirm.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Rerun pytest if needed."],
                        "optional_follow_ups": [],
                    },
                    {
                        "finding_id": "real-change",
                        "severity": "high",
                        "summary": "Behavior is missing.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Implement the missing finalization behavior."],
                        "optional_follow_ups": [],
                    },
                ],
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
    recheck = json.loads(Path(summary["feature_review_recheck_path"]).read_text(encoding="utf-8"))
    assert Path(summary["final_integration_verification_path"]).exists()
    assert result.decision == Decision.NEEDS_REVISION
    assert recheck["accepted_finding_ids"] == ["verification-only"]
    assert recheck["unresolved_finding_ids"] == ["real-change"]

    verification_only_path = supervisor_decision_artifact_path(
        release_bundle_path=result.summary_path.parent,
        decision_type=SupervisorDecisionType.FINAL_REVIEW_FINDING_ADJUDICATION,
        decision_id="v0.1.0__final_review_finding__verification-only",
    )
    real_change_path = supervisor_decision_artifact_path(
        release_bundle_path=result.summary_path.parent,
        decision_type=SupervisorDecisionType.FINAL_REVIEW_FINDING_ADJUDICATION,
        decision_id="v0.1.0__final_review_finding__real-change",
    )
    verification_only = load_supervisor_decision_artifact(verification_only_path)
    real_change = load_supervisor_decision_artifact(real_change_path)
    assert isinstance(verification_only, FinalReviewFindingAdjudicationDecision)
    assert isinstance(real_change, FinalReviewFindingAdjudicationDecision)
    assert verification_only.classification.value == "verification_only"
    assert real_change.classification.value == "blocker"


def test_run_release_feature_review_convergence_limit_final_adjudication_allows_non_blocking_finalization(
    tmp_path: Path,
) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "repo_state_path": "repo_state/demo",
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
            "verification_profiles": {"default": {"commands": ["test -d docs"]}},
            "release_finalization_policy": {"policy": "local_merge", "required_credential_env_vars": []},
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
                "summary": "Verify-only required finding persists.",
                "recommendation": "require_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "verification-only",
                        "severity": "high",
                        "summary": "Verify syntax and rerun pytest to confirm.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Rerun pytest if needed."],
                        "optional_follow_ups": [],
                    },
                    {
                        "finding_id": "follow-up",
                        "severity": "low",
                        "summary": "Consider a backlog follow-up.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": [],
                        "optional_follow_ups": ["Track follow-up work."],
                        "evidence_paths": ["docs/demo-0001.md"],
                    },
                ],
            }
        ),
        FeatureReviewDecision.model_validate(
            {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Still verify-only.",
                "recommendation": "require_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "verification-only",
                        "severity": "high",
                        "summary": "Verify syntax and rerun pytest to confirm.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Rerun pytest if needed."],
                        "optional_follow_ups": [],
                    },
                    {
                        "finding_id": "follow-up",
                        "severity": "low",
                        "summary": "Consider a backlog follow-up.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": [],
                        "optional_follow_ups": ["Track follow-up work."],
                        "evidence_paths": ["docs/demo-0001.md"],
                    },
                ],
            }
        ),
        FeatureReviewDecision.model_validate(
            {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Still verify-only after repairs.",
                "recommendation": "require_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "verification-only",
                        "severity": "high",
                        "summary": "Verify syntax and rerun pytest to confirm.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Rerun pytest if needed."],
                        "optional_follow_ups": [],
                    },
                    {
                        "finding_id": "follow-up",
                        "severity": "low",
                        "summary": "Consider a backlog follow-up.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": [],
                        "optional_follow_ups": ["Track follow-up work."],
                        "evidence_paths": ["docs/demo-0001.md"],
                    },
                ],
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

    from agentic_devloop.feature_review import FeatureReviewConvergenceResult, FeatureReviewFindingConvergenceResult
    from agentic_devloop.models import GeneratedContract

    def fake_convergence(*_args, **_kwargs):
        return FeatureReviewConvergenceResult(
            findings=[
                FeatureReviewFindingConvergenceResult(
                    finding_id="verification-only",
                    classification="blocker",
                    selected_action="repair",
                    matched_previous_finding_id=None,
                    repeated_by_finding_id=True,
                    adjacent_similarity=1.0,
                    verification_false_positive_candidate=False,
                ),
                FeatureReviewFindingConvergenceResult(
                    finding_id="follow-up",
                    classification="backlog_follow_up",
                    selected_action="defer",
                    matched_previous_finding_id=None,
                    repeated_by_finding_id=False,
                    adjacent_similarity=0.25,
                    verification_false_positive_candidate=False,
                ),
            ],
            blocking_finding_ids=["verification-only"],
            accepted_finding_ids=[],
            deferred_finding_ids=["follow-up"],
            false_positive_candidate_ids=[],
        )

    repair_counter = {"n": 0}

    def fake_generate_repairs(*_args, **_kwargs):
        repair_counter["n"] += 1
        task_id = f"repair-{repair_counter['n']:02d}"
        contract = _task_contract(task_id, allowed_files=["docs/demo-0001.md"])
        return [
            GeneratedContract(
                task_id=task_id,
                title=f"Repair {task_id}",
                objective="Apply bounded repair.",
                rationale="Repair contract generated for required finding.",
                suggested_contract=contract,
            )
        ]

    with (
        patch("agentic_devloop.release.invoke_feature_reviewer", side_effect=fake_invoke_feature_reviewer),
        patch("agentic_devloop.release.classify_feature_review_findings_for_convergence", side_effect=fake_convergence),
        patch("agentic_devloop.release.generate_repair_contracts_for_required_findings", side_effect=fake_generate_repairs),
    ):
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
    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    continuation = json.loads(Path(summary["final_review_continuation_decision_path"]).read_text(encoding="utf-8"))
    adjudication_paths = continuation.get("finding_adjudication_paths")
    assert isinstance(adjudication_paths, list)
    assert adjudication_paths
    assert all(Path(path).exists() for path in adjudication_paths)
    assert summary["final_review_continuation_outcome"] in {"accepted_risk", "backlog_follow_up"}
    assert summary["final_review_finding_adjudication_paths"]
    assert all(Path(path).exists() for path in summary["final_review_finding_adjudication_paths"])
    backlog_state = yaml.safe_load((tmp_path / "repo_state" / "demo" / "backlog_state.yaml").read_text(encoding="utf-8"))
    memories = backlog_state["active_epics"][0]["final_review_follow_up_memories"]
    assert memories
    assert {item["classification"] for item in memories} >= {"verification_only", "backlog_follow_up"}
    sample = memories[0]
    assert sample["rationale_summary"]
    assert sample["evidence_paths"]
    assert sample["fallback_plan"]
    assert sample["validators_rerun"]
    assert sample["adjudication_artifact_path"]
    assert sample["continuation_decision_path"]


def test_run_release_feature_review_convergence_limit_final_verification_failure_blocks_accept_and_defer(
    tmp_path: Path,
) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "repo_state_path": "repo_state/demo",
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
            "verification_profiles": {"default": {"commands": ["false"]}, "rerun": {"commands": ["true"]}},
            "feature_review_max_repair_loops": 1,
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
                    "summary": "Still has a verify-only and follow-up finding.",
                    "recommendation": "require_repairs",
                    "accepted_risks": [],
                    "rerun_verification_commands": ["true"],
                    "findings": [
                    {
                        "finding_id": "verification-only",
                        "severity": "high",
                        "summary": "Verify and rerun tests.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Rerun the suite."],
                        "optional_follow_ups": [],
                    },
                    {
                        "finding_id": "follow-up",
                        "severity": "low",
                        "summary": "Consider a backlog follow-up.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": [],
                        "optional_follow_ups": ["Track follow-up work."],
                        "evidence_paths": ["docs/demo-0001.md"],
                    },
                ],
            }
        ),
            FeatureReviewDecision.model_validate(
                {
                    "release_id": "v0.1.0",
                    "reviewer": "strong_model",
                    "summary": "Same findings after repair attempt.",
                    "recommendation": "require_repairs",
                    "accepted_risks": [],
                    "rerun_verification_commands": ["true"],
                    "findings": [
                    {
                        "finding_id": "verification-only",
                        "severity": "high",
                        "summary": "Verify and rerun tests.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Rerun the suite."],
                        "optional_follow_ups": [],
                    },
                    {
                        "finding_id": "follow-up",
                        "severity": "low",
                        "summary": "Consider a backlog follow-up.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": [],
                        "optional_follow_ups": ["Track follow-up work."],
                        "evidence_paths": ["docs/demo-0001.md"],
                    },
                ],
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

    from agentic_devloop.feature_review import FeatureReviewConvergenceResult, FeatureReviewFindingConvergenceResult
    from agentic_devloop.models import GeneratedContract

    def fake_convergence(*_args, **_kwargs):
        return FeatureReviewConvergenceResult(
            findings=[
                FeatureReviewFindingConvergenceResult(
                    finding_id="verification-only",
                    classification="blocker",
                    selected_action="repair",
                    matched_previous_finding_id=None,
                    repeated_by_finding_id=True,
                    adjacent_similarity=1.0,
                    verification_false_positive_candidate=False,
                ),
                FeatureReviewFindingConvergenceResult(
                    finding_id="follow-up",
                    classification="backlog_follow_up",
                    selected_action="defer",
                    matched_previous_finding_id=None,
                    repeated_by_finding_id=False,
                    adjacent_similarity=0.25,
                    verification_false_positive_candidate=False,
                ),
            ],
            blocking_finding_ids=["verification-only"],
            accepted_finding_ids=[],
            deferred_finding_ids=["follow-up"],
            false_positive_candidate_ids=[],
        )

    repair_counter = {"n": 0}

    def fake_generate_repairs(*_args, **_kwargs):
        repair_counter["n"] += 1
        task_id = f"repair-{repair_counter['n']:02d}"
        contract = _task_contract(task_id, allowed_files=["docs/demo-0001.md"])
        return [
            GeneratedContract(
                task_id=task_id,
                title=f"Repair {task_id}",
                objective="Apply bounded repair.",
                rationale="Repair contract generated for required finding.",
                suggested_contract=contract,
            )
        ]

    with (
        patch("agentic_devloop.release.invoke_feature_reviewer", side_effect=fake_invoke_feature_reviewer),
        patch("agentic_devloop.release.classify_feature_review_findings_for_convergence", side_effect=fake_convergence),
        patch("agentic_devloop.release.generate_repair_contracts_for_required_findings", side_effect=fake_generate_repairs),
    ):
        result = run_release(
            project_id="demo",
            release_id="v0.1.0",
            config_dir=config_dir,
            contracts_dir=contracts_dir,
            runs_dir=tmp_path / "runs",
            executor=AllowedFilesExecutor(),
            merge_on_accept=True,
        )

    assert result.decision == Decision.NEEDS_REVISION
    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert Path(summary["final_integration_verification_path"]).exists()
    recheck = json.loads(Path(summary["feature_review_recheck_path"]).read_text(encoding="utf-8"))
    assert recheck["accepted_finding_ids"] == []
    assert recheck["deferred_finding_ids"] == []
    assert recheck["unresolved_finding_ids"] == ["follow-up", "verification-only"]

    follow_up_path = supervisor_decision_artifact_path(
        release_bundle_path=result.summary_path.parent,
        decision_type=SupervisorDecisionType.FINAL_REVIEW_FINDING_ADJUDICATION,
        decision_id="v0.1.0__final_review_finding__follow-up",
    )
    assert follow_up_path.exists()
    loaded = load_supervisor_decision_artifact(follow_up_path)
    assert isinstance(loaded, FinalReviewFindingAdjudicationDecision)
    assert loaded.classification.value == "blocker"


def test_run_release_feature_review_convergence_limit_uses_release_evidence_when_reviewer_evidence_paths_are_omitted(
    tmp_path: Path,
) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "repo_state_path": "repo_state/demo",
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
            "verification_profiles": {"default": {"commands": ["test -d docs"]}},
            "release_finalization_policy": {"policy": "local_merge", "required_credential_env_vars": []},
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
                "summary": "Malformed optional evidence.",
                "recommendation": "require_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "verification-only",
                        "severity": "high",
                        "summary": "Verify syntax and rerun pytest to confirm.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Rerun pytest if needed."],
                        "optional_follow_ups": [],
                    },
                    {
                        "finding_id": "follow-up",
                        "severity": "low",
                        "summary": "Missing evidence paths for backlog follow-up.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": [],
                        "optional_follow_ups": ["Track follow-up work."],
                    },
                ],
            }
        ),
        FeatureReviewDecision.model_validate(
            {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Still malformed evidence.",
                "recommendation": "require_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "verification-only",
                        "severity": "high",
                        "summary": "Verify syntax and rerun pytest to confirm.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Rerun pytest if needed."],
                        "optional_follow_ups": [],
                    },
                    {
                        "finding_id": "follow-up",
                        "severity": "low",
                        "summary": "Missing evidence paths for backlog follow-up.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": [],
                        "optional_follow_ups": ["Track follow-up work."],
                    },
                ],
            }
        ),
        FeatureReviewDecision.model_validate(
            {
                "release_id": "v0.1.0",
                "reviewer": "strong_model",
                "summary": "Still malformed evidence after repairs.",
                "recommendation": "require_repairs",
                "accepted_risks": [],
                "rerun_verification_commands": [],
                "findings": [
                    {
                        "finding_id": "verification-only",
                        "severity": "high",
                        "summary": "Verify syntax and rerun pytest to confirm.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": ["Rerun pytest if needed."],
                        "optional_follow_ups": [],
                    },
                    {
                        "finding_id": "follow-up",
                        "severity": "low",
                        "summary": "Missing evidence paths for backlog follow-up.",
                        "affected_files": ["docs/demo-0001.md"],
                        "required_repairs": [],
                        "optional_follow_ups": ["Track follow-up work."],
                    },
                ],
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

    from agentic_devloop.feature_review import FeatureReviewConvergenceResult, FeatureReviewFindingConvergenceResult
    from agentic_devloop.models import GeneratedContract

    def fake_convergence(*_args, **_kwargs):
        return FeatureReviewConvergenceResult(
            findings=[
                FeatureReviewFindingConvergenceResult(
                    finding_id="verification-only",
                    classification="blocker",
                    selected_action="repair",
                    matched_previous_finding_id=None,
                    repeated_by_finding_id=True,
                    adjacent_similarity=1.0,
                    verification_false_positive_candidate=False,
                ),
                FeatureReviewFindingConvergenceResult(
                    finding_id="follow-up",
                    classification="backlog_follow_up",
                    selected_action="defer",
                    matched_previous_finding_id=None,
                    repeated_by_finding_id=False,
                    adjacent_similarity=0.25,
                    verification_false_positive_candidate=False,
                ),
            ],
            blocking_finding_ids=["verification-only"],
            accepted_finding_ids=[],
            deferred_finding_ids=["follow-up"],
            false_positive_candidate_ids=[],
        )

    repair_counter = {"n": 0}

    def fake_generate_repairs(*_args, **_kwargs):
        repair_counter["n"] += 1
        task_id = f"repair-{repair_counter['n']:02d}"
        contract = _task_contract(task_id, allowed_files=["docs/demo-0001.md"])
        return [
            GeneratedContract(
                task_id=task_id,
                title=f"Repair {task_id}",
                objective="Apply bounded repair.",
                rationale="Repair contract generated for required finding.",
                suggested_contract=contract,
            )
        ]

    with (
        patch("agentic_devloop.release.invoke_feature_reviewer", side_effect=fake_invoke_feature_reviewer),
        patch("agentic_devloop.release.classify_feature_review_findings_for_convergence", side_effect=fake_convergence),
        patch("agentic_devloop.release.generate_repair_contracts_for_required_findings", side_effect=fake_generate_repairs),
    ):
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
    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    continuation = json.loads(Path(summary["final_review_continuation_decision_path"]).read_text(encoding="utf-8"))
    assert continuation["outcome"] in {"accepted_risk", "backlog_follow_up"}
    adjudication_paths = continuation.get("finding_adjudication_paths")
    assert isinstance(adjudication_paths, list)
    assert adjudication_paths
    backlog_state_path = tmp_path / "repo_state" / "demo" / "backlog_state.yaml"
    assert backlog_state_path.exists()
    backlog_state = yaml.safe_load(backlog_state_path.read_text(encoding="utf-8"))
    active_epics = backlog_state.get("active_epics", [])
    memories = active_epics[0].get("final_review_follow_up_memories", []) if active_epics else []
    assert memories
    assert any(item["classification"] == "backlog_follow_up" for item in memories)


def test_persist_compact_final_review_follow_up_memory_returns_none_for_noop(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    continuation_path = tmp_path / "runs" / "final_review_continuation_decision.json"
    continuation_path.parent.mkdir(parents=True)
    decision = FinalReviewContinuationDecision(
        release_id="review-convergence-adjudicator",
        outcome=FinalReviewContinuationOutcome.ACCEPTED_RISK,
        feature_review_path=Path("feature_review.json"),
        feature_review_recheck_path=Path("feature_review_recheck.json"),
        final_integration_verification_path=Path("final_integration_verification.json"),
        finding_ids=["accepted-risk-1"],
        finding_adjudication_paths=[],
        rerun_validator_evidence_paths=[Path("feature_review/verification_rerun_01/verification.log")],
        accepted_risk_rationale="No remaining finding needed compact memory.",
    )
    continuation_path.write_text(decision.model_dump_json(indent=2) + "\n", encoding="utf-8")

    path = _persist_compact_final_review_follow_up_memory(
        config_repo_path=repo,
        repo_state_path=Path("repo_state/demo"),
        release_id="review-convergence-adjudicator",
        continuation_decision_path=continuation_path,
    )

    assert path is None
    assert not (repo / "repo_state" / "demo" / "backlog_state.yaml").exists()


def test_release_finalization_gate_only_counts_required_findings() -> None:
    decision = FeatureReviewDecision.model_validate(
        {
            "release_id": "demo-release",
            "reviewer": "strong_model",
            "summary": "review summary",
            "recommendation": "escalate",
            "findings": [
                {
                    "finding_id": "finding-optional-1",
                    "severity": "moderate",
                    "summary": "optional follow-up only",
                    "affected_files": ["docs/demo.md"],
                    "required_repairs": [],
                    "optional_follow_ups": ["consider tightening wording"],
                }
            ],
        }
    )
    gate = _build_release_finalization_gate(
        decision=Decision.ESCALATED,
        feature_review_decision=decision,
        feature_review_recheck=FeatureReviewRecheckRecord.model_validate(
            {
                "release_id": "demo-release",
                "unresolved_finding_ids": ["finding-optional-1"],
                "resolved_finding_ids": [],
                "accepted_finding_ids": [],
                "stop_reason": "blocked_by_hard_gate",
            }
        ),
    )

    assert gate["allowed"] is False
    assert gate["reason"] == "release_decision_not_accepted"
    assert gate["unresolved_required_finding_ids"] == []


def test_run_release_persists_final_review_continuation_decision_artifact(tmp_path: Path) -> None:
    repo = _repo_with_initial_commit(tmp_path / "repo")
    config_dir = _write_demo_config(tmp_path, repo)
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
            "summary": "Required repair cannot be mapped.",
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
        def __init__(self, value: FeatureReviewDecision) -> None:
            self.decision = value

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
        )

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    continuation_path = Path(summary["final_review_continuation_decision_path"])
    continuation = json.loads(continuation_path.read_text(encoding="utf-8"))

    assert continuation["outcome"] == "hard_stop"
    assert continuation["feature_review_path"] == summary["feature_review_path"]
    assert continuation["feature_review_recheck_path"] == summary["feature_review_recheck_path"]
    assert continuation["final_integration_verification_path"] == summary["final_integration_verification_path"]
    assert continuation["finding_ids"] == []
    assert isinstance(continuation["hard_stop_reason"], str)
    assert continuation["hard_stop_reason"]
    assert Path(summary["final_integration_verification_path"]).exists()
    assert "final_review_continuation_decision_path" in summary
    assert summary["final_review_continuation_outcome"] == continuation["outcome"]
    assert summary["final_review_continuation_finding_ids"] == continuation["finding_ids"]
    assert summary["final_review_finding_adjudication_paths"] == continuation["finding_adjudication_paths"]


def test_final_review_continuation_hard_stop_keeps_unresolved_required_finding_ids(tmp_path: Path) -> None:
    review = FeatureReviewDecision.model_validate(
        {
            "release_id": "v0.1.0",
            "reviewer": "strong_model",
            "summary": "Required repair cannot be mapped.",
            "recommendation": "require_repairs",
            "findings": [
                {
                    "finding_id": "finding-required-1",
                    "severity": "high",
                    "summary": "Fix required in file outside allowed contract scope.",
                    "affected_files": ["src/outside_scope.py"],
                    "required_repairs": ["Apply a code fix."],
                }
            ],
        }
    )

    decision_path = _write_final_review_continuation_decision(
        release_root=tmp_path,
        release_id="v0.1.0",
        feature_review_decision=review,
        feature_review_path=tmp_path / "feature_review.json",
        feature_review_recheck=None,
        feature_review_recheck_path=None,
        feature_review_proposals=[],
        final_integration_verification_path=tmp_path / "final_integration_verification.json",
        final_review_finding_adjudication_paths=[],
        finalization_gate={
            "allowed": False,
            "reason": "release_decision_not_accepted",
            "unresolved_required_finding_ids": [],
        },
    )

    continuation = json.loads(decision_path.read_text(encoding="utf-8"))
    assert continuation["outcome"] == "hard_stop"
    assert continuation["finding_ids"] == ["finding-required-1"]
    assert continuation["hard_stop_reason"] == "missing_generated_repair_contracts"


def test_final_review_continuation_decision_serialized_examples() -> None:
    blocker = FinalReviewContinuationDecision(
        release_id="v0.1.0",
        outcome=FinalReviewContinuationOutcome.BLOCKER,
        feature_review_path=Path("runs/r1/feature_review.json"),
        feature_review_recheck_path=Path("runs/r1/feature_review_recheck.json"),
        finding_ids=["f-blocker"],
        finding_adjudication_paths=[Path("runs/r1/supervisor_decisions/final_review_finding_adjudication__v0.1.0__final_review_finding__f-blocker.json")],
        generated_repair_contract_paths=[Path("runs/r1/feature_review/repairs_01/f-blocker.yaml")],
    )
    accepted_risk = FinalReviewContinuationDecision(
        release_id="v0.1.0",
        outcome=FinalReviewContinuationOutcome.ACCEPTED_RISK,
        feature_review_path=Path("runs/r2/feature_review.json"),
        feature_review_recheck_path=Path("runs/r2/feature_review_recheck.json"),
        final_integration_verification_path=Path("runs/r2/final_integration_verification/final_integration_verification.json"),
        finding_ids=["f-risk"],
        finding_adjudication_paths=[Path("runs/r2/supervisor_decisions/final_review_finding_adjudication__v0.1.0__final_review_finding__f-risk.json")],
        rerun_validator_evidence_paths=[Path("runs/r2/feature_review/verification_rerun_01/verification.log")],
        accepted_risk_rationale="reviewer limitation acknowledged; validators rerun passed",
    )
    backlog_follow_up = FinalReviewContinuationDecision(
        release_id="v0.1.0",
        outcome=FinalReviewContinuationOutcome.BACKLOG_FOLLOW_UP,
        feature_review_path=Path("runs/r3/feature_review.json"),
        feature_review_recheck_path=Path("runs/r3/feature_review_recheck.json"),
        final_integration_verification_path=Path("runs/r3/final_integration_verification/final_integration_verification.json"),
        finding_ids=["f-follow-up"],
        finding_adjudication_paths=[Path("runs/r3/supervisor_decisions/final_review_finding_adjudication__v0.1.0__final_review_finding__f-follow-up.json")],
        backlog_follow_up_proposal_paths=[Path("runs/r3/supervisor_decisions/followup.json")],
    )
    hard_stop = FinalReviewContinuationDecision(
        release_id="v0.1.0",
        outcome=FinalReviewContinuationOutcome.HARD_STOP,
        feature_review_path=Path("runs/r4/feature_review.json"),
        feature_review_recheck_path=Path("runs/r4/feature_review_recheck.json"),
        finding_ids=["f-stop"],
        hard_stop_reason="blocked_by_hard_gate",
    )

    assert blocker.model_dump(mode="json")["outcome"] == "blocker"
    assert accepted_risk.model_dump(mode="json")["outcome"] == "accepted_risk"
    assert backlog_follow_up.model_dump(mode="json")["outcome"] == "backlog_follow_up"
    assert hard_stop.model_dump(mode="json")["outcome"] == "hard_stop"


def test_final_review_continuation_uses_deferred_adjudication_paths_when_proposals_missing(tmp_path: Path) -> None:
    adjudication = FinalReviewFindingAdjudicationDecision.model_validate(
        {
            "decision_id": "v0.1.0__final_review_finding__scope-finding",
            "release_id": "v0.1.0",
            "decided_at": datetime.now(UTC),
            "decided_by": "test",
            "rationale": "Scope expansion should be deferred after final verification passes.",
            "evidence_paths": [str((tmp_path / "final_integration_verification.json").resolve())],
            "finding_id": "scope-finding",
            "classification": "scope_expansion",
            "selected_action": "defer",
            "outcome": "stop",
            "fallback_plan": "Track a follow-up release if scope is later approved.",
            "validators_to_rerun": ["integration_verification"],
        }
    )
    adjudication_path = write_supervisor_decision_artifact(
        release_bundle_path=tmp_path,
        decision=adjudication,
    )

    decision_path = _write_final_review_continuation_decision(
        release_root=tmp_path,
        release_id="v0.1.0",
        feature_review_decision=None,
        feature_review_path=tmp_path / "feature_review.json",
        feature_review_recheck=FeatureReviewRecheckRecord(
            release_id="v0.1.0",
            unresolved_finding_ids=[],
            resolved_finding_ids=[],
            accepted_finding_ids=[],
            deferred_finding_ids=["scope-finding"],
            stop_reason="accepted_with_rationale",
        ),
        feature_review_recheck_path=tmp_path / "feature_review_recheck.json",
        feature_review_proposals=[],
        final_integration_verification_path=tmp_path / "final_integration_verification.json",
        final_review_finding_adjudication_paths=[adjudication_path],
        finalization_gate={
            "allowed": True,
            "reason": "allowed",
            "unresolved_required_finding_ids": [],
        },
    )

    continuation = json.loads(decision_path.read_text(encoding="utf-8"))
    assert continuation["outcome"] == "backlog_follow_up"
    assert continuation["backlog_follow_up_proposal_paths"] == [str(adjudication_path)]
    assert continuation["finding_ids"] == ["scope-finding"]


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
    release_finalization_policy: dict | None = None,
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
            "release_finalization_policy": release_finalization_policy
            if release_finalization_policy is not None
            else {"policy": "local_merge", "required_credential_env_vars": []},
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
