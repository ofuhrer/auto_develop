from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from agentic_devloop.backlog import BacklogPlannerBackendResult, parse_backlog_planner_output, plan_backlog
from agentic_devloop.backlog import run_backlog
import agentic_devloop.governor as governor_module
from agentic_devloop.execution_strategy import (
    ExecutionStrategyAction,
    ExecutionStrategyReason,
    ExecutionStrategySelection,
)
from agentic_devloop.governor import GovernorLoop
from agentic_devloop.models import (
    ExecutorResult,
    GovernorContinuationAction,
    GovernorContinuationStopReason,
    GovernorStopReason,
)
from agentic_devloop.state_refresh import (
    PostCycleStateRefreshArtifact,
    build_post_cycle_state_refresh,
    write_post_cycle_state_refresh_artifact,
)
from agentic_devloop.state_store import StateStore


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_plan_backlog_prioritizes_goal_and_writes_objective(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
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
                "model": "worker",
                "max_walltime_minutes": 5,
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
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text(
        """
# Roadmap

Remaining Phase 3 work is now small:

1. Rename or alias domain-specific public terms to generic validation terminology.
2. Make repository instruction ingestion more explicit so target repos can declare workflow requirements.
3. Add optional PR creation or PR-preparation automation for the final feature branch.
""".lstrip(),
        encoding="utf-8",
    )

    result = plan_backlog(
        project_id="demo",
        goal="Make the autonomous agent read repository instructions and choose the next epic.",
        roadmap_path=roadmap,
        config_dir=config_dir,
        runs_dir=tmp_path / "runs",
        objectives_dir=tmp_path / "objectives",
        write_objective=True,
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    assert result.plan_path.exists()
    assert result.objective_path is not None
    assert result.objective_path.exists()
    assert result.plan.selected_epic_id == "epic-0001"
    assert "repository instruction ingestion" in result.plan.epics[0].objective
    assert result.plan.epics[0].suggested_release_id.startswith("demo-20260512-")

    plan_payload = json.loads(result.plan_path.read_text(encoding="utf-8"))
    objective_payload = yaml.safe_load(result.objective_path.read_text(encoding="utf-8"))
    assert plan_payload["selected_epic_id"] == "epic-0001"
    assert objective_payload["release_id"] == result.plan.epics[0].suggested_release_id


def test_plan_backlog_can_execute_agent_backend(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
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
                "model": "worker",
                "max_walltime_minutes": 5,
            },
            "model_roles": {
                "planner": {
                    "type": "codex_cli",
                    "model": "planner",
                    "max_walltime_minutes": 5,
                }
            },
            "model_routing": {"default_role": "planner"},
            "verification_profiles": {"default": {"commands": ["true"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text("# Roadmap\n\nRemaining work:\n\n1. Add run-backlog.\n", encoding="utf-8")
    repo_state = repo / "repo_state" / "demo"
    repo_state.mkdir(parents=True)
    (repo_state / "backlog_state.yaml").write_text("active_goal: autonomous governor\n", encoding="utf-8")
    config_path = config_dir / "demo.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["repo_state_path"] = "repo_state/demo"
    _write_yaml(config_path, config)
    backend = FakeBacklogBackend(tmp_path)

    result = plan_backlog(
        project_id="demo",
        goal="Let an agent choose the next epic.",
        roadmap_path=roadmap,
        config_dir=config_dir,
        runs_dir=tmp_path / "runs",
        mode="strong-model",
        planner_backend=backend,
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    assert backend.seen_model == "planner"
    assert "Autonomous Roadmap Governor" in backend.seen_prompt
    assert "Repo State:" in backend.seen_prompt
    assert "active_goal: autonomous governor" in backend.seen_prompt
    assert result.plan.planner == "strong-model"
    assert result.plan.selected_epic_id == "epic-0001"
    assert result.plan.planner_stdout_path is not None
    assert result.plan.planner_stdout_path.name == "backlog_planner_stdout.log"


def test_parse_backlog_planner_output_validates_selected_epic() -> None:
    try:
        parse_backlog_planner_output(
            {
                "project_id": "demo",
                "goal": "Goal.",
                "roadmap_path": "ROADMAP.md",
                "selected_epic_id": "missing",
                "epics": [],
            },
            project_id="demo",
        )
    except ValueError as error:
        assert "selected_epic_id not found" in str(error)
    else:
        raise AssertionError("expected invalid selected epic to fail")


class FakeBacklogBackend:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.seen_prompt = ""
        self.seen_model = ""

    def with_output_dir(self, output_dir: Path):
        self.output_dir = output_dir
        return self

    def generate(self, *, prompt: str, goal: str, roadmap_text: str, model: str):
        self.seen_prompt = prompt
        self.seen_model = model
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = self.output_dir / "backlog_planner_stdout.log"
        stderr_path = self.output_dir / "backlog_planner_stderr.log"
        metadata_path = self.output_dir / "backlog_planner_metadata.json"
        stdout_path.write_text("agent backlog stdout\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        metadata_path.write_text("{}\n", encoding="utf-8")
        return BacklogPlannerBackendResult(
            raw_output={
                "project_id": "demo",
                "goal": goal,
                "roadmap_path": "ROADMAP.md",
                "planner": "strong-model",
                "selected_epic_id": "epic-0001",
                "epics": [
                    {
                        "epic_id": "epic-0001",
                        "title": "Agent-selected roadmap governor",
                        "objective": "Implement the highest reward governor increment.",
                        "rationale": "The agent selected this from docs and roadmap.",
                        "priority": 1,
                        "source_refs": ["ROADMAP.md"],
                        "acceptance_criteria": ["Governor objective is generated."],
                        "suggested_release_id": "demo-agent-selected-governor",
                    }
                ],
                "warnings": [],
            },
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            metadata_path=metadata_path,
        )


class FakeObjectivePlannerBackend:
    def generate(self, *, prompt: str, objective, existing_contracts, model):
        assert "Strong Release Planning Prompt" in prompt
        assert objective.release_id == "demo-agent-selected-governor"
        assert existing_contracts == []
        assert model == "planner"
        return {
            "release_id": objective.release_id,
            "planner": "strong-model",
            "generated_contracts": [
                {
                    "task_id": "objective-0001",
                    "title": "Create objective docs",
                    "objective": "Create one objective evidence document.",
                    "rationale": "Covers the objective with one bounded docs task.",
                    "suggested_contract": {
                        "task_id": "objective-0001",
                        "release_id": objective.release_id,
                        "title": "Create objective docs",
                        "task_type": "documentation",
                        "budget_class": "S",
                        "objective": "Create docs/objective.md.",
                        "allowed_files": ["docs/objective.md"],
                        "forbidden_changes": ["Do not edit source files."],
                        "required_evidence": ["git diff", "test output"],
                        "verification": {"commands": ["test -f docs/objective.md"]},
                        "stop_conditions": ["Verification fails."],
                    },
                }
            ],
            "warnings": [],
        }


class FakeExecutor:
    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = worktree_path / "docs" / "objective.md"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("# Objective\n", encoding="utf-8")
        stdout_path = output_dir / "executor_stdout.log"
        stderr_path = output_dir / "executor_stderr.log"
        stdout_path.write_text(f"used {prompt_path}\n", encoding="utf-8")
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


def test_run_backlog_selects_one_epic_reuses_objective_and_runs_release(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
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
                "model": "worker",
                "max_walltime_minutes": 5,
            },
            "model_roles": {
                "planner": {
                    "type": "codex_cli",
                    "model": "planner",
                    "max_walltime_minutes": 5,
                }
            },
            "model_routing": {"default_role": "planner"},
            "verification_profiles": {"default": {"commands": ["true"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )

    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text("# Roadmap\n\nRemaining work:\n\n1. Add run-backlog.\n", encoding="utf-8")

    objectives_dir = tmp_path / "objectives"
    objectives_dir.mkdir()
    existing_objective = objectives_dir / "demo-agent-selected-governor.yaml"
    _write_yaml(
        existing_objective,
        {
            "release_id": "demo-agent-selected-governor",
            "title": "Existing objective",
            "objective": "Existing objective body.",
            "acceptance_criteria": ["Governor objective is generated."],
        },
    )

    result = run_backlog(
        project_id="demo",
        goal="Let an agent choose the next epic.",
        roadmap_path=roadmap,
        selected_epic_id="epic-0001",
        config_dir=config_dir,
        contracts_dir=tmp_path / "contracts",
        runs_dir=tmp_path / "runs",
        objectives_dir=objectives_dir,
        mode="strong-model",
        planner_backend=FakeBacklogBackend(tmp_path),
        objective_planner_backend=FakeObjectivePlannerBackend(),
        executor=FakeExecutor(),
    )

    assert result.selected_epic_id == "epic-0001"
    assert result.plan_path.exists()
    assert result.backlog_plan_path == result.plan_path
    assert result.objective_path == existing_objective
    assert result.generated_objective_path is None
    assert result.objective.release_id == "demo-agent-selected-governor"
    assert result.contract_plan_path is not None
    assert result.contract_plan_path.exists()
    assert result.execution_strategy_selection_path is not None
    assert result.execution_strategy_selection_path.exists()
    assert result.supervisor_decision_path is not None
    assert result.supervisor_decision_path.exists()
    assert result.one_shot_execution_input_path is None
    selection = json.loads(result.execution_strategy_selection_path.read_text(encoding="utf-8"))
    assert selection["selected_action"] == "sequential_contracts"
    assert selection["reason"] == "coupled_sequential"
    assert result.release_id == "demo-agent-selected-governor"
    assert result.release.decision == "accepted"
    assert result.release_summary_path == result.release.summary_path
    assert result.release_metrics_path == result.release.metrics_path
    assert result.release_budget_path == result.release.budget_path
    assert result.release_tuning_path == result.release.tuning_path
    assert result.release_summary_path is not None and result.release_summary_path.exists()
    assert result.release_metrics_path is not None and result.release_metrics_path.exists()
    assert result.release_budget_path is not None and result.release_budget_path.exists()
    assert result.release_tuning_path is not None and result.release_tuning_path.exists()
    assert result.evidence_manifest is not None
    assert result.evidence_manifest.backlog_plan_path == result.plan_path
    assert result.evidence_manifest.generated_objective_path is None
    assert result.evidence_manifest.contract_plan_path == result.contract_plan_path
    assert result.evidence_manifest.execution_strategy_selection_path == result.execution_strategy_selection_path
    assert result.evidence_manifest.supervisor_decision_path == result.supervisor_decision_path
    assert result.evidence_manifest.one_shot_execution_input_path == result.one_shot_execution_input_path
    assert result.evidence_manifest.release_summary_path == result.release_summary_path
    assert result.evidence_manifest.release_log_path == result.release.log_path
    assert result.evidence_manifest.release_review_path == result.release.review_path
    assert result.evidence_manifest.release_metrics_path == result.release_metrics_path
    assert result.evidence_manifest.release_budget_path == result.release_budget_path
    assert result.evidence_manifest.release_tuning_path == result.release_tuning_path


def test_run_backlog_records_generated_objective_path_when_objective_is_created(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
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
                "model": "worker",
                "max_walltime_minutes": 5,
            },
            "model_roles": {
                "planner": {
                    "type": "codex_cli",
                    "model": "planner",
                    "max_walltime_minutes": 5,
                }
            },
            "model_routing": {"default_role": "planner"},
            "verification_profiles": {"default": {"commands": ["true"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )

    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text("# Roadmap\n\nRemaining work:\n\n1. Add run-backlog.\n", encoding="utf-8")
    objectives_dir = tmp_path / "objectives"

    result = run_backlog(
        project_id="demo",
        goal="Let an agent choose the next epic.",
        roadmap_path=roadmap,
        selected_epic_id="epic-0001",
        config_dir=config_dir,
        contracts_dir=tmp_path / "contracts",
        runs_dir=tmp_path / "runs",
        objectives_dir=objectives_dir,
        mode="strong-model",
        planner_backend=FakeBacklogBackend(tmp_path),
        objective_planner_backend=FakeObjectivePlannerBackend(),
        executor=FakeExecutor(),
    )

    assert result.generated_objective_path == result.objective_path
    assert result.generated_objective_path is not None and result.generated_objective_path.exists()
    assert result.execution_strategy_selection_path is not None
    assert result.execution_strategy_selection_path.exists()
    assert result.supervisor_decision_path is not None
    assert result.supervisor_decision_path.exists()
    assert result.one_shot_execution_input_path is None
    assert result.evidence_manifest is not None
    assert result.evidence_manifest.backlog_plan_path == result.backlog_plan_path
    assert result.evidence_manifest.generated_objective_path == result.generated_objective_path
    assert result.evidence_manifest.contract_plan_path == result.contract_plan_path
    assert result.evidence_manifest.execution_strategy_selection_path == result.execution_strategy_selection_path
    assert result.evidence_manifest.supervisor_decision_path == result.supervisor_decision_path
    assert result.evidence_manifest.one_shot_execution_input_path == result.one_shot_execution_input_path
    assert result.evidence_manifest.release_summary_path == result.release_summary_path
    assert result.evidence_manifest.release_log_path == result.release.log_path
    assert result.evidence_manifest.release_review_path == result.release.review_path
    assert result.evidence_manifest.release_metrics_path == result.release_metrics_path
    assert result.evidence_manifest.release_budget_path == result.release_budget_path
    assert result.evidence_manifest.release_tuning_path == result.release_tuning_path


def test_run_backlog_fails_clearly_when_planner_backend_raises(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
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
            "executor": {"type": "codex_cli", "model": "worker", "max_walltime_minutes": 5},
            "model_roles": {
                "planner": {"type": "codex_cli", "model": "planner", "max_walltime_minutes": 5}
            },
            "model_routing": {"default_role": "planner"},
            "verification_profiles": {"default": {"commands": ["true"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )

    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text("# Roadmap\n\nRemaining work:\n\n1. Add run-backlog.\n", encoding="utf-8")

    class FailingBacklogBackend(FakeBacklogBackend):
        def generate(self, *, prompt: str, goal: str, roadmap_text: str, model: str):
            raise RuntimeError("backlog planner command failed (codex exec): backend exploded")

    with pytest.raises(RuntimeError, match="backlog planner command failed"):
        run_backlog(
            project_id="demo",
            goal="Let an agent choose the next epic.",
            roadmap_path=roadmap,
            selected_epic_id="epic-0001",
            config_dir=config_dir,
            contracts_dir=tmp_path / "contracts",
            runs_dir=tmp_path / "runs",
            objectives_dir=tmp_path / "objectives",
            mode="strong-model",
            planner_backend=FailingBacklogBackend(tmp_path),
            objective_planner_backend=FakeObjectivePlannerBackend(),
            executor=FakeExecutor(),
        )


def test_run_backlog_fails_on_invalid_planner_output(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
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
            "executor": {"type": "codex_cli", "model": "worker", "max_walltime_minutes": 5},
            "model_roles": {
                "planner": {"type": "codex_cli", "model": "planner", "max_walltime_minutes": 5}
            },
            "model_routing": {"default_role": "planner"},
            "verification_profiles": {"default": {"commands": ["true"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )

    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text("# Roadmap\n\nRemaining work:\n\n1. Add run-backlog.\n", encoding="utf-8")

    class InvalidBacklogBackend(FakeBacklogBackend):
        def generate(self, *, prompt: str, goal: str, roadmap_text: str, model: str):
            self.output_dir.mkdir(parents=True, exist_ok=True)
            stdout_path = self.output_dir / "backlog_planner_stdout.log"
            stderr_path = self.output_dir / "backlog_planner_stderr.log"
            metadata_path = self.output_dir / "backlog_planner_metadata.json"
            stdout_path.write_text("{}", encoding="utf-8")
            stderr_path.write_text("", encoding="utf-8")
            metadata_path.write_text("{}\n", encoding="utf-8")
            return BacklogPlannerBackendResult(
                raw_output={"project_id": "demo", "selected_epic_id": "missing", "epics": []},
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                metadata_path=metadata_path,
            )

    with pytest.raises(ValueError, match="backlog planner output did not match the BacklogPlan schema"):
        run_backlog(
            project_id="demo",
            goal="Let an agent choose the next epic.",
            roadmap_path=roadmap,
            selected_epic_id="epic-0001",
            config_dir=config_dir,
            contracts_dir=tmp_path / "contracts",
            runs_dir=tmp_path / "runs",
            objectives_dir=tmp_path / "objectives",
            mode="strong-model",
            planner_backend=InvalidBacklogBackend(tmp_path),
            objective_planner_backend=FakeObjectivePlannerBackend(),
            executor=FakeExecutor(),
        )


def test_run_backlog_propagates_run_objective_failure(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
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
            "executor": {"type": "codex_cli", "model": "worker", "max_walltime_minutes": 5},
            "model_roles": {
                "planner": {"type": "codex_cli", "model": "planner", "max_walltime_minutes": 5}
            },
            "model_routing": {"default_role": "planner"},
            "verification_profiles": {"default": {"commands": ["true"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text("# Roadmap\n\nRemaining work:\n\n1. Add run-backlog.\n", encoding="utf-8")

    def failing_run_objective(**_kwargs):
        raise RuntimeError("run-objective failed: release execution failed")

    monkeypatch.setattr("agentic_devloop.backlog.run_objective", failing_run_objective)

    with pytest.raises(RuntimeError, match="run-objective failed: release execution failed"):
        run_backlog(
            project_id="demo",
            goal="Let an agent choose the next epic.",
            roadmap_path=roadmap,
            selected_epic_id="epic-0001",
            config_dir=config_dir,
            contracts_dir=tmp_path / "contracts",
            runs_dir=tmp_path / "runs",
            objectives_dir=tmp_path / "objectives",
            mode="strong-model",
            planner_backend=FakeBacklogBackend(tmp_path),
            objective_planner_backend=FakeObjectivePlannerBackend(),
            executor=FakeExecutor(),
        )


def test_governor_loop_runs_one_epic_and_builds_evidence_manifest(tmp_path) -> None:
    from types import SimpleNamespace

    runs_dir = tmp_path / "runs"
    objectives_dir = tmp_path / "objectives"
    roadmap_path = tmp_path / "ROADMAP.md"
    roadmap_path.write_text("# Roadmap\n", encoding="utf-8")
    config_dir = _write_project_config(tmp_path)

    plan_path = runs_dir / "backlog_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("{}", encoding="utf-8")

    plan = parse_backlog_planner_output(
        {
            "project_id": "demo",
            "goal": "Goal.",
            "roadmap_path": str(roadmap_path),
            "planner": "deterministic",
            "selected_epic_id": "epic-0001",
            "warnings": [],
            "epics": [
                {
                    "epic_id": "epic-0001",
                    "title": "One epic",
                    "objective": "Do the epic.",
                    "rationale": "Because.",
                    "priority": 1,
                    "source_refs": ["roadmap:1"],
                    "acceptance_criteria": ["It works."],
                    "suggested_release_id": "demo-one-epic",
                }
            ],
        },
        project_id="demo",
    )

    def fake_plan_backlog(**_kwargs):
        return SimpleNamespace(plan_path=plan_path, plan=plan, objective_path=None)

    contract_plan_path = runs_dir / "contract_plan.json"
    release_summary_path = runs_dir / "release_summary.json"
    release_metrics_path = runs_dir / "release_metrics.json"
    release_budget_path = runs_dir / "release_budget.json"
    release_tuning_path = runs_dir / "release_tuning.json"
    for path in [
        contract_plan_path,
        release_summary_path,
        release_metrics_path,
        release_budget_path,
        release_tuning_path,
    ]:
        path.write_text("{}", encoding="utf-8")

    def fake_run_objective(**_kwargs):
        return SimpleNamespace(
            release_id="demo-one-epic",
            planning=SimpleNamespace(plan_path=contract_plan_path),
            release=SimpleNamespace(
                summary_path=release_summary_path,
                metrics_path=release_metrics_path,
                budget_path=release_budget_path,
                tuning_path=release_tuning_path,
                decision="accepted",
            ),
        )

    result = GovernorLoop(plan_backlog=fake_plan_backlog, run_objective=fake_run_objective).run_one_epic(
        project_id="demo",
        goal="Run one epic.",
        roadmap_path=roadmap_path,
        selected_epic_id=None,
        config_dir=config_dir,
        contracts_dir=tmp_path / "contracts",
        runs_dir=runs_dir,
        objectives_dir=objectives_dir,
        mode="deterministic",
        planner_backend=None,
        objective_planner_backend=FakeObjectivePlannerBackend(),
        executor=FakeExecutor(),
        verification_timeout_seconds=60,
        allow_dirty=True,
        commit_on_accept=False,
        merge_on_accept=False,
        push_on_accept=False,
        release_finalize="none",
        integration_branch=None,
        stop_on_failure=True,
        execution_mode="sequential",
        debug_keep_artifacts=False,
        progress=None,
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    assert result.selected_epic_id == "epic-0001"
    assert result.objective.release_id == "demo-one-epic"
    assert result.objective_path.exists()
    assert result.generated_objective_path == result.objective_path
    assert result.evidence_manifest is not None
    assert result.evidence_manifest.backlog_plan_path == plan_path
    assert result.evidence_manifest.generated_objective_path == result.objective_path
    assert result.evidence_manifest.contract_plan_path == contract_plan_path
    assert result.evidence_manifest.release_summary_path == release_summary_path
    assert result.evidence_manifest.release_log_path is None
    assert result.evidence_manifest.release_review_path is None
    assert result.state_refresh_summary_path is not None
    assert result.state_refresh_summary_path.exists()
    summary_payload = json.loads(result.state_refresh_summary_path.read_text(encoding="utf-8"))
    assert summary_payload["state_review_snapshot_path"].endswith("state_review_snapshot.json")
    assert summary_payload["status_count"] >= 0


def test_governor_loop_state_refresh_failure_writes_error_artifact(tmp_path, monkeypatch) -> None:
    runs_dir = tmp_path / "runs"
    objectives_dir = tmp_path / "objectives"
    roadmap_path = tmp_path / "ROADMAP.md"
    roadmap_path.write_text("# Roadmap\n", encoding="utf-8")
    config_dir = _write_project_config(tmp_path)

    def fail_state_refresh(**_kwargs):
        raise ValueError("git status unavailable")

    monkeypatch.setattr(governor_module, "collect_state_review_snapshot", fail_state_refresh)

    with pytest.raises(RuntimeError, match="governor state refresh failed before epic selection") as exc:
        GovernorLoop(
            plan_backlog=lambda **_kwargs: pytest.fail("planning should not run after state refresh failure"),
            run_objective=lambda **_kwargs: pytest.fail("objective should not run after state refresh failure"),
        ).run_one_epic(
            project_id="demo",
            goal="Run one epic.",
            roadmap_path=roadmap_path,
            selected_epic_id=None,
            config_dir=config_dir,
            contracts_dir=tmp_path / "contracts",
            runs_dir=runs_dir,
            objectives_dir=objectives_dir,
            mode="deterministic",
            planner_backend=None,
            objective_planner_backend=FakeObjectivePlannerBackend(),
            executor=FakeExecutor(),
            verification_timeout_seconds=60,
            allow_dirty=True,
            commit_on_accept=False,
            merge_on_accept=False,
            push_on_accept=False,
            release_finalize="none",
            integration_branch=None,
            stop_on_failure=True,
            execution_mode="sequential",
            debug_keep_artifacts=False,
            progress=None,
            now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
        )

    error_path = Path(str(exc.value).split("error_artifact=", 1)[1])
    payload = json.loads(error_path.read_text(encoding="utf-8"))
    assert payload["phase"] == "collect_state_review_snapshot"
    assert payload["error_type"] == "ValueError"
    assert payload["error"] == "git status unavailable"
    assert payload["partial_artifact_paths"] == []


def test_governor_loop_marks_planning_only_strategy_as_reviewed_not_blocked(tmp_path) -> None:
    from types import SimpleNamespace

    runs_dir = tmp_path / "runs"
    objectives_dir = tmp_path / "objectives"
    roadmap_path = tmp_path / "ROADMAP.md"
    roadmap_path.write_text("# Roadmap\n", encoding="utf-8")
    config_dir = _write_project_config(tmp_path)

    plan_path = runs_dir / "backlog_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("{}", encoding="utf-8")
    plan = parse_backlog_planner_output(
        {
            "project_id": "demo",
            "goal": "Goal.",
            "roadmap_path": str(roadmap_path),
            "planner": "deterministic",
            "selected_epic_id": "epic-0001",
            "warnings": [],
            "epics": [
                {
                    "epic_id": "epic-0001",
                    "title": "One epic",
                    "objective": "Do the epic.",
                    "rationale": "Because.",
                    "priority": 1,
                    "source_refs": ["roadmap:1"],
                    "acceptance_criteria": ["It works."],
                    "suggested_release_id": "demo-one-epic",
                }
            ],
        },
        project_id="demo",
    )

    def fake_plan_backlog(**_kwargs):
        return SimpleNamespace(plan_path=plan_path, plan=plan, objective_path=None)

    contract_plan_path = runs_dir / "contract_plan.json"
    contract_plan_path.write_text("{}", encoding="utf-8")
    selection = ExecutionStrategySelection(
        release_id="demo-one-epic",
        selected_action=ExecutionStrategyAction.ONE_SHOT,
        reason=ExecutionStrategyReason.COHESIVE_ONE_SHOT,
    )

    def fake_run_objective(**_kwargs):
        return SimpleNamespace(
            release_id="demo-one-epic",
            planning=SimpleNamespace(
                plan_path=contract_plan_path,
                execution_strategy_selection=selection,
                execution_strategy_selection_path=runs_dir / "execution_strategy_selection.json",
                supervisor_decision_path=runs_dir / "supervisor_decision.json",
                one_shot_execution_input_path=runs_dir / "one_shot_execution_input.json",
            ),
            release=None,
        )

    state_store = StateStore(tmp_path / "repo_state" / "demo" / "backlog_state.yaml")
    GovernorLoop(
        plan_backlog=fake_plan_backlog,
        run_objective=fake_run_objective,
        state_store=state_store,
    ).run_one_epic(
        project_id="demo",
        goal="Run one epic.",
        roadmap_path=roadmap_path,
        selected_epic_id=None,
        config_dir=config_dir,
        contracts_dir=tmp_path / "contracts",
        runs_dir=runs_dir,
        objectives_dir=objectives_dir,
        mode="deterministic",
        planner_backend=None,
        objective_planner_backend=FakeObjectivePlannerBackend(),
        executor=FakeExecutor(),
        verification_timeout_seconds=60,
        allow_dirty=True,
        commit_on_accept=False,
        merge_on_accept=False,
        push_on_accept=False,
        release_finalize="none",
        integration_branch=None,
        stop_on_failure=True,
        execution_mode="sequential",
        debug_keep_artifacts=False,
        progress=None,
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    state = state_store.load()
    assert state.blocked_epics == []
    assert state.active_epic == "epic-0001"
    assert [record.epic_id for record in state.reviewed_epics] == ["epic-0001"]
    assert state.reviewed_epics[0].status_reason == "execution-strategy:one_shot"


def test_governor_loop_runs_multiple_epic_cycles_and_records_state(tmp_path) -> None:
    from types import SimpleNamespace

    runs_dir = tmp_path / "runs"
    objectives_dir = tmp_path / "objectives"
    roadmap_path = tmp_path / "ROADMAP.md"
    roadmap_path.write_text("# Roadmap\n", encoding="utf-8")
    config_dir = _write_project_config(tmp_path)
    plan_calls: list[str] = []
    run_calls: list[Path] = []

    def plan_for(epic_id: str, release_id: str):
        plan_path = runs_dir / f"{epic_id}" / "backlog_plan.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(
            plan_path=plan_path,
            objective_path=None,
            plan=parse_backlog_planner_output(
                {
                    "project_id": "demo",
                    "goal": "Goal.",
                    "roadmap_path": str(roadmap_path),
                    "planner": "deterministic",
                    "selected_epic_id": epic_id,
                    "warnings": [],
                    "epics": [
                        {
                            "epic_id": epic_id,
                            "title": f"Epic {epic_id}",
                            "objective": f"Do {epic_id}.",
                            "rationale": "Because.",
                            "priority": 1,
                            "source_refs": ["roadmap:1"],
                            "acceptance_criteria": ["It works."],
                            "suggested_release_id": release_id,
                        }
                    ],
                },
                project_id="demo",
            ),
        )

    plans = [
        plan_for("epic-0001", "demo-epic-0001"),
        plan_for("epic-0002", "demo-epic-0002"),
    ]

    def fake_plan_backlog(**_kwargs):
        plan_calls.append("called")
        return plans[len(plan_calls) - 1]

    def fake_run_objective(**kwargs):
        objective_path = kwargs["objective_path"]
        run_calls.append(objective_path)
        release_id = objective_path.stem
        release_summary_path = runs_dir / release_id / "release_summary.json"
        release_summary_path.parent.mkdir(parents=True, exist_ok=True)
        release_summary_path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(
            release_id=release_id,
            planning=SimpleNamespace(plan_path=runs_dir / release_id / "contract_plan.json"),
            release=SimpleNamespace(
                release_id=release_id,
                summary_path=release_summary_path,
                metrics_path=runs_dir / release_id / "release_metrics.json",
                budget_path=runs_dir / release_id / "release_budget.json",
                tuning_path=runs_dir / release_id / "release_tuning.md",
                decision="accepted",
            ),
        )

    state_store = StateStore(tmp_path / "repo_state" / "demo" / "backlog_state.yaml")
    result = GovernorLoop(
        plan_backlog=fake_plan_backlog,
        run_objective=fake_run_objective,
        state_store=state_store,
    ).run_epics(
        project_id="demo",
        goal="Run two epics.",
        roadmap_path=roadmap_path,
        selected_epic_id=None,
        epic_count=2,
        config_dir=config_dir,
        contracts_dir=tmp_path / "contracts",
        runs_dir=runs_dir,
        objectives_dir=objectives_dir,
        mode="deterministic",
        planner_backend=None,
        objective_planner_backend=FakeObjectivePlannerBackend(),
        executor=FakeExecutor(),
        verification_timeout_seconds=60,
        allow_dirty=True,
        commit_on_accept=False,
        merge_on_accept=False,
        push_on_accept=False,
        release_finalize="none",
        integration_branch=None,
        stop_on_failure=True,
        execution_mode="sequential",
        debug_keep_artifacts=False,
        progress=None,
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    assert result.attempted_epic_count == 2
    assert result.accepted_epic_count == 2
    assert result.stop_reason == GovernorStopReason.REQUESTED_EPIC_COUNT_REACHED
    assert [cycle.selected_epic_id for cycle in result.cycles] == ["epic-0001", "epic-0002"]
    assert [path.stem for path in run_calls] == ["demo-epic-0001", "demo-epic-0002"]
    state = state_store.load()
    assert state.completed_epics == ["epic-0001", "epic-0002"]
    assert [summary.release_id for summary in state.recent_run_summaries] == ["demo-epic-0002", "demo-epic-0001"]
    assert all(cycle.state_refresh_summary_path is not None for cycle in result.cycles)


def test_governor_loop_distinguishes_attempted_and_accepted_counts(tmp_path) -> None:
    from types import SimpleNamespace

    runs_dir = tmp_path / "runs"
    objectives_dir = tmp_path / "objectives"
    roadmap_path = tmp_path / "ROADMAP.md"
    roadmap_path.write_text("# Roadmap\n", encoding="utf-8")
    config_dir = _write_project_config(tmp_path)

    plan_path = runs_dir / "epic-0001" / "backlog_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("{}", encoding="utf-8")
    plan = parse_backlog_planner_output(
        {
            "project_id": "demo",
            "goal": "Goal.",
            "roadmap_path": str(roadmap_path),
            "planner": "deterministic",
            "selected_epic_id": "epic-0001",
            "warnings": [],
            "epics": [
                {
                    "epic_id": "epic-0001",
                    "title": "Epic 1",
                    "objective": "Do epic 1.",
                    "rationale": "Because.",
                    "priority": 1,
                    "source_refs": ["roadmap:1"],
                    "acceptance_criteria": ["It works."],
                    "suggested_release_id": "demo-epic-0001",
                }
            ],
        },
        project_id="demo",
    )

    def fake_plan_backlog(**_kwargs):
        return SimpleNamespace(plan_path=plan_path, plan=plan, objective_path=None)

    def fake_run_objective(**kwargs):
        objective_path = kwargs["objective_path"]
        release_summary_path = runs_dir / objective_path.stem / "release_summary.json"
        release_summary_path.parent.mkdir(parents=True, exist_ok=True)
        release_summary_path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(
            release_id=objective_path.stem,
            planning=SimpleNamespace(plan_path=runs_dir / objective_path.stem / "contract_plan.json"),
            release=SimpleNamespace(
                release_id=objective_path.stem,
                summary_path=release_summary_path,
                metrics_path=runs_dir / objective_path.stem / "release_metrics.json",
                budget_path=runs_dir / objective_path.stem / "release_budget.json",
                tuning_path=runs_dir / objective_path.stem / "release_tuning.md",
                decision="needs_revision",
            ),
        )

    result = GovernorLoop(plan_backlog=fake_plan_backlog, run_objective=fake_run_objective).run_epics(
        project_id="demo",
        goal="Run one epic.",
        roadmap_path=roadmap_path,
        selected_epic_id=None,
        epic_count=2,
        config_dir=config_dir,
        contracts_dir=tmp_path / "contracts",
        runs_dir=runs_dir,
        objectives_dir=objectives_dir,
        mode="deterministic",
        planner_backend=None,
        objective_planner_backend=FakeObjectivePlannerBackend(),
        executor=FakeExecutor(),
        verification_timeout_seconds=60,
        allow_dirty=True,
        commit_on_accept=False,
        merge_on_accept=False,
        push_on_accept=False,
        release_finalize="none",
        integration_branch=None,
        stop_on_failure=True,
        execution_mode="sequential",
        debug_keep_artifacts=False,
        progress=None,
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    assert result.attempted_epic_count == 1
    assert result.accepted_epic_count == 0
    assert result.stop_reason == GovernorStopReason.RELEASE_NOT_ACCEPTED


def test_governor_loop_stops_on_blocked_finalization_before_next_cycle(tmp_path) -> None:
    from types import SimpleNamespace

    runs_dir = tmp_path / "runs"
    objectives_dir = tmp_path / "objectives"
    roadmap_path = tmp_path / "ROADMAP.md"
    roadmap_path.write_text("# Roadmap\n", encoding="utf-8")
    config_dir = _write_project_config(tmp_path)
    run_objective_calls = 0

    plan_path = runs_dir / "epic-0001" / "backlog_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("{}", encoding="utf-8")
    plan = parse_backlog_planner_output(
        {
            "project_id": "demo",
            "goal": "Goal.",
            "roadmap_path": str(roadmap_path),
            "planner": "deterministic",
            "selected_epic_id": "epic-0001",
            "warnings": [],
            "epics": [
                {
                    "epic_id": "epic-0001",
                    "title": "Epic 1",
                    "objective": "Do epic 1.",
                    "rationale": "Because.",
                    "priority": 1,
                    "source_refs": ["roadmap:1"],
                    "acceptance_criteria": ["It works."],
                    "suggested_release_id": "demo-epic-0001",
                }
            ],
        },
        project_id="demo",
    )

    def fake_plan_backlog(**_kwargs):
        return SimpleNamespace(plan_path=plan_path, plan=plan, objective_path=None)

    def fake_run_objective(**kwargs):
        nonlocal run_objective_calls
        run_objective_calls += 1
        objective_path = kwargs["objective_path"]
        release_summary_path = runs_dir / objective_path.stem / "release_summary.json"
        release_summary_path.parent.mkdir(parents=True, exist_ok=True)
        release_summary_path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(
            release_id=objective_path.stem,
            planning=SimpleNamespace(plan_path=runs_dir / objective_path.stem / "contract_plan.json"),
            release=SimpleNamespace(
                release_id=objective_path.stem,
                summary_path=release_summary_path,
                metrics_path=runs_dir / objective_path.stem / "release_metrics.json",
                budget_path=runs_dir / objective_path.stem / "release_budget.json",
                tuning_path=runs_dir / objective_path.stem / "release_tuning.md",
                decision="accepted",
                finalization_gate={
                    "allowed": False,
                    "reason": "unresolved_required_findings",
                    "unresolved_required_finding_ids": ["finding-1"],
                    "decision": "accepted",
                },
                finalization=None,
            ),
        )

    result = GovernorLoop(plan_backlog=fake_plan_backlog, run_objective=fake_run_objective).run_epics(
        project_id="demo",
        goal="Run one epic.",
        roadmap_path=roadmap_path,
        selected_epic_id=None,
        epic_count=2,
        config_dir=config_dir,
        contracts_dir=tmp_path / "contracts",
        runs_dir=runs_dir,
        objectives_dir=objectives_dir,
        mode="deterministic",
        planner_backend=None,
        objective_planner_backend=FakeObjectivePlannerBackend(),
        executor=FakeExecutor(),
        verification_timeout_seconds=60,
        allow_dirty=True,
        commit_on_accept=False,
        merge_on_accept=False,
        push_on_accept=False,
        release_finalize="push-feature",
        integration_branch=None,
        stop_on_failure=True,
        execution_mode="sequential",
        debug_keep_artifacts=False,
        progress=None,
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    assert run_objective_calls == 1
    assert result.attempted_epic_count == 1
    assert result.stop_reason == GovernorStopReason.BLOCKED_FINALIZATION
    assert result.cycles[0].blocked_finalization is not None
    assert result.cycles[0].blocked_finalization["type"] == "finalization_gate_blocked"


def test_governor_persists_blocked_finalization_memory_before_second_cycle(tmp_path) -> None:
    from types import SimpleNamespace

    runs_dir = tmp_path / "runs"
    objectives_dir = tmp_path / "objectives"
    roadmap_path = tmp_path / "ROADMAP.md"
    roadmap_path.write_text("# Roadmap\n", encoding="utf-8")
    config_dir = _write_project_config(tmp_path)
    run_objective_calls = 0
    plan_calls = 0

    plan_path = runs_dir / "epic-0001" / "backlog_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("{}", encoding="utf-8")
    plan = parse_backlog_planner_output(
        {
            "project_id": "demo",
            "goal": "Goal.",
            "roadmap_path": str(roadmap_path),
            "planner": "deterministic",
            "selected_epic_id": "epic-0001",
            "warnings": [],
            "epics": [
                {
                    "epic_id": "epic-0001",
                    "title": "Epic 1",
                    "objective": "Do epic 1.",
                    "rationale": "Because.",
                    "priority": 1,
                    "source_refs": ["roadmap:1"],
                    "acceptance_criteria": ["It works."],
                    "suggested_release_id": "demo-epic-0001",
                }
            ],
        },
        project_id="demo",
    )

    def fake_plan_backlog(**_kwargs):
        nonlocal plan_calls
        plan_calls += 1
        return SimpleNamespace(plan_path=plan_path, plan=plan, objective_path=None)

    def fake_run_objective(**kwargs):
        nonlocal run_objective_calls
        run_objective_calls += 1
        objective_path = kwargs["objective_path"]
        release_dir = runs_dir / objective_path.stem
        release_dir.mkdir(parents=True, exist_ok=True)
        release_summary_path = release_dir / "release_summary.json"
        release_summary_path.write_text(
            json.dumps(
                {
                    "integration_branch": "feature/demo-epic-0001",
                    "integration_commit": "deadbeef",
                    "cleanup_report_path": str(release_dir / "cleanup_report.json"),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return SimpleNamespace(
            release_id=objective_path.stem,
            planning=SimpleNamespace(plan_path=runs_dir / objective_path.stem / "contract_plan.json"),
            release=SimpleNamespace(
                release_id=objective_path.stem,
                summary_path=release_summary_path,
                metrics_path=release_dir / "release_metrics.json",
                budget_path=release_dir / "release_budget.json",
                tuning_path=release_dir / "release_tuning.md",
                decision="accepted",
                finalization_gate={
                    "allowed": False,
                    "reason": "unresolved_required_findings",
                    "unresolved_required_finding_ids": ["finding-1"],
                    "decision": "accepted",
                },
                finalization=None,
            ),
        )

    state_store = StateStore(tmp_path / "repo_state" / "demo" / "backlog_state.yaml")
    result = GovernorLoop(
        plan_backlog=fake_plan_backlog,
        run_objective=fake_run_objective,
        state_store=state_store,
    ).run_epics(
        project_id="demo",
        goal="Run one epic.",
        roadmap_path=roadmap_path,
        selected_epic_id=None,
        epic_count=2,
        config_dir=config_dir,
        contracts_dir=tmp_path / "contracts",
        runs_dir=runs_dir,
        objectives_dir=objectives_dir,
        mode="deterministic",
        planner_backend=None,
        objective_planner_backend=FakeObjectivePlannerBackend(),
        executor=FakeExecutor(),
        verification_timeout_seconds=60,
        allow_dirty=True,
        commit_on_accept=False,
        merge_on_accept=False,
        push_on_accept=False,
        release_finalize="push-feature",
        integration_branch=None,
        stop_on_failure=True,
        execution_mode="sequential",
        debug_keep_artifacts=False,
        progress=None,
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    assert plan_calls == 1
    assert run_objective_calls == 1
    assert result.stop_reason == GovernorStopReason.BLOCKED_FINALIZATION
    state = state_store.load()
    blocked_record = state.blocked_epic_records[0]
    assert blocked_record.epic_id == "epic-0001"
    assert blocked_record.blocked_reason == "unresolved_required_findings"
    assert len(blocked_record.finalization_outcome_references) == 1
    finalization_memory = blocked_record.finalization_outcome_references[0]
    assert finalization_memory.release_id == "demo-epic-0001"
    assert finalization_memory.outcome == "blocked"
    assert str(finalization_memory.run_summary_path).endswith("release_summary.json")
    assert finalization_memory.branch == "feature/demo-epic-0001"
    assert finalization_memory.commit == "deadbeef"
    assert str(finalization_memory.cleanup_report_path).endswith("cleanup_report.json")
    assert finalization_memory.unresolved_finding_ids == ["finding-1"]
    assert finalization_memory.recommended_backlog_state == "blocked"


def test_state_refresh_builder_completed_finalized_with_review_details(tmp_path) -> None:
    from types import SimpleNamespace

    release_dir = tmp_path / "runs" / "demo-epic-0001_release"
    release_dir.mkdir(parents=True, exist_ok=True)
    feature_review_path = release_dir / "feature_review.json"
    feature_review_path.write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "finding_id": "finding-1",
                        "severity": "moderate",
                        "summary": "Required file overlap remains unresolved.",
                    }
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )
    feature_review_recheck_path = release_dir / "feature_review_recheck.json"
    feature_review_recheck_path.write_text(
        json.dumps({"unresolved_finding_ids": ["finding-1"]}) + "\n",
        encoding="utf-8",
    )
    release_summary_path = release_dir / "release_summary.json"
    release_summary_path.write_text(
        json.dumps(
            {
                "integration_branch": "feature/demo-epic-0001",
                "integration_commit": "deadbeef",
                "cleanup_report_path": str(release_dir / "cleanup_report.json"),
                "feature_review_path": str(feature_review_path),
                "feature_review_recheck_path": str(feature_review_recheck_path),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = SimpleNamespace(
        selected_epic_id="epic-0001",
        release_id="demo-epic-0001",
        release=SimpleNamespace(decision="accepted"),
        release_summary_path=release_summary_path,
        release_metrics_path=release_dir / "release_metrics.json",
        release_budget_path=release_dir / "release_budget.json",
        release_tuning_path=release_dir / "release_tuning.md",
        finalization_policy="push-feature",
        finalization_result={"result": {"merged": True, "pushed": True}},
        blocked_finalization=None,
        governor_cycle_continuation=None,
    )

    artifact, refresh_outcome = build_post_cycle_state_refresh(result=result, retry_count=1, repair_count=2)
    validated = PostCycleStateRefreshArtifact.model_validate(artifact.model_dump(mode="json"))

    assert validated.lifecycle_state == "completed"
    assert validated.status_reason == "accepted_and_finalized"
    assert len(validated.unresolved_finding_references) == 1
    assert validated.unresolved_finding_references[0].finding_id == "finding-1"
    assert validated.unresolved_finding_references[0].summary == "Required file overlap remains unresolved."
    assert validated.unresolved_finding_references[0].source_path == feature_review_path
    assert refresh_outcome.finalization_outcome_references[0].branch == "feature/demo-epic-0001"
    assert refresh_outcome.finalization_outcome_references[0].commit == "deadbeef"


def test_state_refresh_builder_blocked_finalization_from_gate(tmp_path) -> None:
    from types import SimpleNamespace

    release_dir = tmp_path / "runs" / "demo-epic-0002_release"
    release_dir.mkdir(parents=True, exist_ok=True)
    release_summary_path = release_dir / "release_summary.json"
    release_summary_path.write_text(json.dumps({"integration_branch": "feature/demo-epic-0002"}) + "\n", encoding="utf-8")
    result = SimpleNamespace(
        selected_epic_id="epic-0002",
        release_id="demo-epic-0002",
        release=SimpleNamespace(decision="accepted"),
        release_summary_path=release_summary_path,
        release_metrics_path=None,
        release_budget_path=None,
        release_tuning_path=None,
        finalization_policy="push-feature",
        finalization_result={"gate": {"allowed": False}},
        blocked_finalization={
            "type": "finalization_gate_blocked",
            "reason": "unresolved_required_findings",
            "unresolved_required_finding_ids": ["finding-2"],
        },
        governor_cycle_continuation=None,
    )

    artifact, refresh_outcome = build_post_cycle_state_refresh(result=result)
    assert artifact.lifecycle_state == "blocked"
    assert artifact.status_reason == "blocked:unresolved_required_findings"
    assert artifact.unresolved_finding_references[0].finding_id == "finding-2"
    assert refresh_outcome.blocked_reason == "unresolved_required_findings"
    assert refresh_outcome.finalization_outcome_references[0].outcome == "blocked"


def test_state_refresh_builder_failed_release_marks_blocked(tmp_path) -> None:
    from types import SimpleNamespace

    release_dir = tmp_path / "runs" / "demo-epic-0003_release"
    release_dir.mkdir(parents=True, exist_ok=True)
    result = SimpleNamespace(
        selected_epic_id="epic-0003",
        release_id="demo-epic-0003",
        release=SimpleNamespace(decision="failed"),
        release_summary_path=release_dir / "release_summary.json",
        release_metrics_path=None,
        release_budget_path=None,
        release_tuning_path=None,
        finalization_policy="local-merge",
        finalization_result=None,
        blocked_finalization=None,
        governor_cycle_continuation=None,
    )

    artifact, refresh_outcome = build_post_cycle_state_refresh(result=result)
    assert artifact.lifecycle_state == "blocked"
    assert artifact.status_reason == "release_failed"
    assert refresh_outcome.lifecycle_state == "blocked"
    assert refresh_outcome.outcome_references[0].outcome == "failed"


def test_state_refresh_builder_manual_merge_completed_and_writes_artifact(tmp_path) -> None:
    from types import SimpleNamespace

    release_dir = tmp_path / "runs" / "demo-epic-0004_release"
    release_dir.mkdir(parents=True, exist_ok=True)
    release_summary_path = release_dir / "release_summary.json"
    release_summary_path.write_text(
        json.dumps({"integration_branch": "feature/demo-epic-0004", "integration_commit": "cafe1234"}) + "\n",
        encoding="utf-8",
    )
    result = SimpleNamespace(
        selected_epic_id="epic-0004",
        release_id="demo-epic-0004",
        release=SimpleNamespace(decision="accepted"),
        release_summary_path=release_summary_path,
        release_metrics_path=release_dir / "release_metrics.json",
        release_budget_path=release_dir / "release_budget.json",
        release_tuning_path=release_dir / "release_tuning.md",
        finalization_policy="stop_missing_policy_or_credentials",
        finalization_result={"result": {"merged": False, "pushed": False}},
        blocked_finalization=None,
        governor_cycle_continuation=None,
    )

    artifact, refresh_outcome = build_post_cycle_state_refresh(result=result)
    written = write_post_cycle_state_refresh_artifact(artifact=artifact, artifacts_dir=release_dir)
    payload = json.loads(written.read_text(encoding="utf-8"))

    assert artifact.lifecycle_state == "completed"
    assert artifact.status_reason == "accepted_manual_merge_or_completed"
    assert "record manual merge/completion outcome in repo-state memory" in artifact.next_recommendations
    assert payload["status_reason"] == "accepted_manual_merge_or_completed"
    assert payload["finalization_outcome_path"].endswith("release_summary.json")
    assert refresh_outcome.finalization_outcome_references[0].recommended_backlog_state == "completed"


def test_governor_cycle_continuation_records_unresolved_review_stop_reason(tmp_path) -> None:
    from types import SimpleNamespace

    runs_dir = tmp_path / "runs"
    objectives_dir = tmp_path / "objectives"
    roadmap_path = tmp_path / "ROADMAP.md"
    roadmap_path.write_text("# Roadmap\n", encoding="utf-8")
    config_dir = _write_project_config(tmp_path)

    plan_path = runs_dir / "epic-0001" / "backlog_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("{}", encoding="utf-8")
    plan = parse_backlog_planner_output(
        {
            "project_id": "demo",
            "goal": "Goal.",
            "roadmap_path": str(roadmap_path),
            "planner": "deterministic",
            "selected_epic_id": "epic-0001",
            "warnings": [],
            "epics": [
                {
                    "epic_id": "epic-0001",
                    "title": "Epic 1",
                    "objective": "Do epic 1.",
                    "rationale": "Because.",
                    "priority": 1,
                    "source_refs": ["roadmap:1"],
                    "acceptance_criteria": ["It works."],
                    "suggested_release_id": "demo-epic-0001",
                }
            ],
        },
        project_id="demo",
    )

    def fake_plan_backlog(**_kwargs):
        return SimpleNamespace(plan_path=plan_path, plan=plan, objective_path=None)

    def fake_run_objective(**kwargs):
        objective_path = kwargs["objective_path"]
        release_dir = runs_dir / objective_path.stem
        release_dir.mkdir(parents=True, exist_ok=True)
        release_summary_path = release_dir / "release_summary.json"
        review_path = release_dir / "feature_review.json"
        recheck_path = release_dir / "feature_review_recheck.json"
        review_path.write_text(
            json.dumps(
                {
                    "release_id": objective_path.stem,
                    "reviewer": "strong_model",
                    "summary": "Accepted with rationale.",
                    "recommendation": "approve_with_repairs",
                    "accepted_risks": ["accepted risk"],
                    "rerun_verification_commands": [],
                    "findings": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        recheck_path.write_text(
            json.dumps(
                {
                    "release_id": objective_path.stem,
                    "unresolved_finding_ids": ["finding-1"],
                    "resolved_finding_ids": [],
                    "accepted_finding_ids": [],
                    "deferred_finding_ids": [],
                    "stop_reason": "blocked_by_retry_budget",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        release_summary_path.write_text(
            json.dumps(
                {
                    "release_id": objective_path.stem,
                    "feature_review_path": str(review_path),
                    "feature_review_recheck_path": str(recheck_path),
                    "feature_review_proposals": [],
                    "finalization_gate": {
                        "allowed": False,
                        "reason": "unresolved_required_findings",
                        "unresolved_required_finding_ids": ["finding-1"],
                        "decision": "accepted",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return SimpleNamespace(
            release_id=objective_path.stem,
            planning=SimpleNamespace(plan_path=release_dir / "contract_plan.json"),
            release=SimpleNamespace(
                release_id=objective_path.stem,
                summary_path=release_summary_path,
                metrics_path=release_dir / "release_metrics.json",
                budget_path=release_dir / "release_budget.json",
                tuning_path=release_dir / "release_tuning.md",
                decision="accepted",
                finalization_gate={
                    "allowed": False,
                    "reason": "unresolved_required_findings",
                    "unresolved_required_finding_ids": ["finding-1"],
                    "decision": "accepted",
                },
                finalization=None,
            ),
        )

    result = GovernorLoop(plan_backlog=fake_plan_backlog, run_objective=fake_run_objective).run_epics(
        project_id="demo",
        goal="Run one epic.",
        roadmap_path=roadmap_path,
        selected_epic_id=None,
        epic_count=2,
        config_dir=config_dir,
        contracts_dir=tmp_path / "contracts",
        runs_dir=runs_dir,
        objectives_dir=objectives_dir,
        mode="deterministic",
        planner_backend=None,
        objective_planner_backend=FakeObjectivePlannerBackend(),
        executor=FakeExecutor(),
        verification_timeout_seconds=60,
        allow_dirty=True,
        commit_on_accept=False,
        merge_on_accept=False,
        push_on_accept=False,
        release_finalize="push-feature",
        integration_branch=None,
        stop_on_failure=True,
        execution_mode="sequential",
        debug_keep_artifacts=False,
        progress=None,
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    continuation = result.cycles[0].governor_cycle_continuation
    assert continuation is not None
    assert continuation.action == GovernorContinuationAction.STOP
    assert continuation.stop_reason == GovernorContinuationStopReason.EXHAUSTED_REPAIR_BUDGET
    assert continuation.feature_review is not None
    assert continuation.feature_review.unresolved_finding_ids == ["finding-1"]


def test_governor_cycle_continuation_records_accepted_with_rationale_and_backlog_follow_ups(tmp_path) -> None:
    from types import SimpleNamespace

    runs_dir = tmp_path / "runs"
    objectives_dir = tmp_path / "objectives"
    roadmap_path = tmp_path / "ROADMAP.md"
    roadmap_path.write_text("# Roadmap\n", encoding="utf-8")
    config_dir = _write_project_config(tmp_path)

    plan_path = runs_dir / "epic-0001" / "backlog_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("{}", encoding="utf-8")
    plan = parse_backlog_planner_output(
        {
            "project_id": "demo",
            "goal": "Goal.",
            "roadmap_path": str(roadmap_path),
            "planner": "deterministic",
            "selected_epic_id": "epic-0001",
            "warnings": [],
            "epics": [
                {
                    "epic_id": "epic-0001",
                    "title": "Epic 1",
                    "objective": "Do epic 1.",
                    "rationale": "Because.",
                    "priority": 1,
                    "source_refs": ["roadmap:1"],
                    "acceptance_criteria": ["It works."],
                    "suggested_release_id": "demo-epic-0001",
                }
            ],
        },
        project_id="demo",
    )

    def fake_plan_backlog(**_kwargs):
        return SimpleNamespace(plan_path=plan_path, plan=plan, objective_path=None)

    def fake_run_objective(**kwargs):
        objective_path = kwargs["objective_path"]
        release_dir = runs_dir / objective_path.stem
        release_dir.mkdir(parents=True, exist_ok=True)
        release_summary_path = release_dir / "release_summary.json"
        review_path = release_dir / "feature_review.json"
        recheck_path = release_dir / "feature_review_recheck.json"
        review_path.write_text(
            json.dumps(
                {
                    "release_id": objective_path.stem,
                    "reviewer": "strong_model",
                    "summary": "Accepted with rationale.",
                    "recommendation": "approve_with_repairs",
                    "accepted_risks": ["accepted risk rationale"],
                    "rerun_verification_commands": [],
                    "findings": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        recheck_path.write_text(
            json.dumps(
                {
                    "release_id": objective_path.stem,
                    "unresolved_finding_ids": [],
                    "resolved_finding_ids": [],
                    "accepted_finding_ids": ["finding-2"],
                    "deferred_finding_ids": ["finding-3"],
                    "stop_reason": "accepted_with_rationale",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        release_summary_path.write_text(
            json.dumps(
                {
                    "release_id": objective_path.stem,
                    "feature_review_path": str(review_path),
                    "feature_review_recheck_path": str(recheck_path),
                    "feature_review_proposals": [
                        {
                            "finding_id": "finding-3",
                            "classification": "backlog_follow_up",
                            "selected_action": "defer",
                            "decision_artifact_path": str(release_dir / "proposal.json"),
                            "matched_previous_finding_id": None,
                            "attempt": 1,
                        }
                    ],
                    "finalization_gate": {
                        "allowed": True,
                        "reason": "allowed",
                        "unresolved_required_finding_ids": [],
                        "decision": "accepted",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return SimpleNamespace(
            release_id=objective_path.stem,
            planning=SimpleNamespace(plan_path=release_dir / "contract_plan.json"),
            release=SimpleNamespace(
                release_id=objective_path.stem,
                summary_path=release_summary_path,
                metrics_path=release_dir / "release_metrics.json",
                budget_path=release_dir / "release_budget.json",
                tuning_path=release_dir / "release_tuning.md",
                decision="accepted",
                finalization_gate={
                    "allowed": True,
                    "reason": "allowed",
                    "unresolved_required_finding_ids": [],
                    "decision": "accepted",
                },
                finalization=SimpleNamespace(error=None, failed_step=None, merged=False, pushed=False),
            ),
        )

    result = GovernorLoop(plan_backlog=fake_plan_backlog, run_objective=fake_run_objective).run_epics(
        project_id="demo",
        goal="Run one epic.",
        roadmap_path=roadmap_path,
        selected_epic_id=None,
        epic_count=1,
        config_dir=config_dir,
        contracts_dir=tmp_path / "contracts",
        runs_dir=runs_dir,
        objectives_dir=objectives_dir,
        mode="deterministic",
        planner_backend=None,
        objective_planner_backend=FakeObjectivePlannerBackend(),
        executor=FakeExecutor(),
        verification_timeout_seconds=60,
        allow_dirty=True,
        commit_on_accept=False,
        merge_on_accept=False,
        push_on_accept=False,
        release_finalize="none",
        integration_branch=None,
        stop_on_failure=True,
        execution_mode="sequential",
        debug_keep_artifacts=False,
        progress=None,
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    continuation = result.cycles[0].governor_cycle_continuation
    assert continuation is not None
    assert continuation.action == GovernorContinuationAction.CONTINUE
    assert continuation.feature_review is not None
    assert continuation.feature_review.recheck_stop_reason == "accepted_with_rationale"
    assert continuation.feature_review.accepted_finding_ids == ["finding-2"]
    assert continuation.feature_review.accepted_risks == ["accepted risk rationale"]
    assert len(continuation.feature_review.backlog_follow_up_proposals) == 1


def test_governor_loop_stops_with_no_actionable_work_for_completed_epic_before_objective_handoff(tmp_path) -> None:
    from types import SimpleNamespace

    runs_dir = tmp_path / "runs"
    objectives_dir = tmp_path / "objectives"
    roadmap_path = tmp_path / "ROADMAP.md"
    roadmap_path.write_text("# Roadmap\n", encoding="utf-8")
    config_dir = _write_project_config(tmp_path)
    run_objective_calls = 0

    plan_path = runs_dir / "epic-0001" / "backlog_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("{}", encoding="utf-8")
    plan = parse_backlog_planner_output(
        {
            "project_id": "demo",
            "goal": "Goal.",
            "roadmap_path": str(roadmap_path),
            "planner": "deterministic",
            "selected_epic_id": "epic-0001",
            "warnings": [],
            "epics": [
                {
                    "epic_id": "epic-0001",
                    "title": "Epic 1",
                    "objective": "Do epic 1.",
                    "rationale": "Because.",
                    "priority": 1,
                    "source_refs": ["roadmap:1"],
                    "acceptance_criteria": ["It works."],
                    "suggested_release_id": "demo-epic-0001",
                }
            ],
        },
        project_id="demo",
    )

    def fake_plan_backlog(**_kwargs):
        return SimpleNamespace(plan_path=plan_path, plan=plan, objective_path=None)

    def fake_run_objective(**_kwargs):
        nonlocal run_objective_calls
        run_objective_calls += 1
        raise AssertionError("run_objective should not be called for no actionable work")

    state_store = StateStore(tmp_path / "repo_state" / "demo" / "backlog_state.yaml")
    state_store.mark_completed_epic("epic-0001")

    result = GovernorLoop(
        plan_backlog=fake_plan_backlog,
        run_objective=fake_run_objective,
        state_store=state_store,
    ).run_epics(
        project_id="demo",
        goal="Run one epic.",
        roadmap_path=roadmap_path,
        selected_epic_id=None,
        epic_count=1,
        config_dir=config_dir,
        contracts_dir=tmp_path / "contracts",
        runs_dir=runs_dir,
        objectives_dir=objectives_dir,
        mode="deterministic",
        planner_backend=None,
        objective_planner_backend=FakeObjectivePlannerBackend(),
        executor=FakeExecutor(),
        verification_timeout_seconds=60,
        allow_dirty=True,
        commit_on_accept=False,
        merge_on_accept=False,
        push_on_accept=False,
        release_finalize="none",
        integration_branch=None,
        stop_on_failure=True,
        execution_mode="sequential",
        debug_keep_artifacts=False,
        progress=None,
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    assert run_objective_calls == 0
    assert result.stop_reason == GovernorStopReason.NO_ACTIONABLE_WORK
    assert result.attempted_epic_count == 1
    assert result.accepted_epic_count == 0
    assert len(result.cycles) == 1
    assert result.cycles[0].release is None
    assert result.cycles[0].backlog_plan_path == plan_path
    assert result.cycles[0].state_refresh_summary_path is not None
    assert result.cycles[0].evidence_manifest is not None
    assert result.cycles[0].evidence_manifest.state_refresh_summary_path is not None


def test_governor_loop_stops_on_repeated_epic_when_no_state_store_guard_exists(tmp_path) -> None:
    from types import SimpleNamespace

    runs_dir = tmp_path / "runs"
    objectives_dir = tmp_path / "objectives"
    roadmap_path = tmp_path / "ROADMAP.md"
    roadmap_path.write_text("# Roadmap\n", encoding="utf-8")
    config_dir = _write_project_config(tmp_path)

    def plan_for(index: int):
        plan_path = runs_dir / f"epic-{index}" / "backlog_plan.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(
            plan_path=plan_path,
            objective_path=None,
            plan=parse_backlog_planner_output(
                {
                    "project_id": "demo",
                    "goal": "Goal.",
                    "roadmap_path": str(roadmap_path),
                    "planner": "deterministic",
                    "selected_epic_id": "epic-0001",
                    "warnings": [],
                    "epics": [
                        {
                            "epic_id": "epic-0001",
                            "title": "Epic 1",
                            "objective": "Do epic 1.",
                            "rationale": "Because.",
                            "priority": 1,
                            "source_refs": ["roadmap:1"],
                            "acceptance_criteria": ["It works."],
                            "suggested_release_id": "demo-epic-0001",
                        }
                    ],
                },
                project_id="demo",
            ),
        )

    plans = [plan_for(1), plan_for(2)]
    call_count = 0

    def fake_plan_backlog(**_kwargs):
        nonlocal call_count
        planned = plans[call_count]
        call_count += 1
        return planned

    def fake_run_objective(**kwargs):
        objective_path = kwargs["objective_path"]
        release_id = objective_path.stem
        release_summary_path = runs_dir / release_id / "release_summary.json"
        release_summary_path.parent.mkdir(parents=True, exist_ok=True)
        release_summary_path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(
            release_id=release_id,
            planning=SimpleNamespace(plan_path=runs_dir / release_id / "contract_plan.json"),
            release=SimpleNamespace(
                release_id=release_id,
                summary_path=release_summary_path,
                metrics_path=runs_dir / release_id / "release_metrics.json",
                budget_path=runs_dir / release_id / "release_budget.json",
                tuning_path=runs_dir / release_id / "release_tuning.md",
                decision="accepted",
            ),
        )

    result = GovernorLoop(plan_backlog=fake_plan_backlog, run_objective=fake_run_objective).run_epics(
        project_id="demo",
        goal="Run repeated epic.",
        roadmap_path=roadmap_path,
        selected_epic_id=None,
        epic_count=2,
        config_dir=config_dir,
        contracts_dir=tmp_path / "contracts",
        runs_dir=runs_dir,
        objectives_dir=objectives_dir,
        mode="deterministic",
        planner_backend=None,
        objective_planner_backend=FakeObjectivePlannerBackend(),
        executor=FakeExecutor(),
        verification_timeout_seconds=60,
        allow_dirty=True,
        commit_on_accept=False,
        merge_on_accept=False,
        push_on_accept=False,
        release_finalize="none",
        integration_branch=None,
        stop_on_failure=True,
        execution_mode="sequential",
        debug_keep_artifacts=False,
        progress=None,
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    assert result.attempted_epic_count == 2
    assert result.accepted_epic_count == 2
    assert result.stop_reason == GovernorStopReason.REPEATED_EPIC_SELECTED


def test_governor_loop_applies_completed_refresh_before_next_planning_cycle(tmp_path) -> None:
    from types import SimpleNamespace

    runs_dir = tmp_path / "runs"
    objectives_dir = tmp_path / "objectives"
    roadmap_path = tmp_path / "ROADMAP.md"
    roadmap_path.write_text("# Roadmap\n", encoding="utf-8")
    config_dir = _write_project_config(tmp_path)
    state_store = StateStore(tmp_path / "repo_state" / "demo" / "backlog_state.yaml")
    plan_calls = 0

    def _plan(epic_id: str, release_id: str):
        plan_path = runs_dir / f"{epic_id}" / "backlog_plan.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(
            plan_path=plan_path,
            objective_path=None,
            plan=parse_backlog_planner_output(
                {
                    "project_id": "demo",
                    "goal": "Goal.",
                    "roadmap_path": str(roadmap_path),
                    "planner": "deterministic",
                    "selected_epic_id": epic_id,
                    "warnings": [],
                    "epics": [
                        {
                            "epic_id": epic_id,
                            "title": f"Epic {epic_id}",
                            "objective": f"Do {epic_id}.",
                            "rationale": "Because.",
                            "priority": 1,
                            "source_refs": ["roadmap:1"],
                            "acceptance_criteria": ["It works."],
                            "suggested_release_id": release_id,
                        }
                    ],
                },
                project_id="demo",
            ),
        )

    plans = [_plan("epic-0001", "demo-epic-0001"), _plan("epic-0002", "demo-epic-0002")]

    def fake_plan_backlog(**_kwargs):
        nonlocal plan_calls
        if plan_calls == 1:
            assert "epic-0001" in state_store.load().completed_epics
        planned = plans[plan_calls]
        plan_calls += 1
        return planned

    def fake_run_objective(**kwargs):
        release_id = kwargs["objective_path"].stem
        release_dir = runs_dir / release_id
        release_dir.mkdir(parents=True, exist_ok=True)
        summary_path = release_dir / "release_summary.json"
        summary_path.write_text(
            json.dumps({"integration_branch": f"feature/{release_id}", "integration_commit": "deadbeef"}) + "\n",
            encoding="utf-8",
        )
        return SimpleNamespace(
            release_id=release_id,
            planning=SimpleNamespace(plan_path=release_dir / "contract_plan.json"),
            release=SimpleNamespace(
                release_id=release_id,
                summary_path=summary_path,
                metrics_path=release_dir / "release_metrics.json",
                budget_path=release_dir / "release_budget.json",
                tuning_path=release_dir / "release_tuning.md",
                decision="accepted",
                finalization_gate={"allowed": True, "reason": "allowed", "decision": "accepted"},
                finalization=SimpleNamespace(error=None, failed_step=None, merged=True, pushed=True),
            ),
        )

    result = GovernorLoop(
        plan_backlog=fake_plan_backlog,
        run_objective=fake_run_objective,
        state_store=state_store,
    ).run_epics(
        project_id="demo",
        goal="Run two epics.",
        roadmap_path=roadmap_path,
        selected_epic_id=None,
        epic_count=2,
        config_dir=config_dir,
        contracts_dir=tmp_path / "contracts",
        runs_dir=runs_dir,
        objectives_dir=objectives_dir,
        mode="deterministic",
        planner_backend=None,
        objective_planner_backend=FakeObjectivePlannerBackend(),
        executor=FakeExecutor(),
        verification_timeout_seconds=60,
        allow_dirty=True,
        commit_on_accept=False,
        merge_on_accept=False,
        push_on_accept=False,
        release_finalize="push-feature",
        integration_branch=None,
        stop_on_failure=True,
        execution_mode="sequential",
        debug_keep_artifacts=False,
        progress=None,
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    assert result.stop_reason == GovernorStopReason.REQUESTED_EPIC_COUNT_REACHED
    assert plan_calls == 2


def test_governor_loop_applies_blocked_refresh_reason_before_next_planning_cycle(tmp_path) -> None:
    from types import SimpleNamespace

    runs_dir = tmp_path / "runs"
    objectives_dir = tmp_path / "objectives"
    roadmap_path = tmp_path / "ROADMAP.md"
    roadmap_path.write_text("# Roadmap\n", encoding="utf-8")
    config_dir = _write_project_config(tmp_path)
    state_store = StateStore(tmp_path / "repo_state" / "demo" / "backlog_state.yaml")
    plan_calls = 0

    first_plan_path = runs_dir / "epic-0001" / "backlog_plan.json"
    first_plan_path.parent.mkdir(parents=True, exist_ok=True)
    first_plan_path.write_text("{}", encoding="utf-8")
    second_plan_path = runs_dir / "epic-0002" / "backlog_plan.json"
    second_plan_path.parent.mkdir(parents=True, exist_ok=True)
    second_plan_path.write_text("{}", encoding="utf-8")

    def _plan(epic_id: str, plan_path: Path):
        return SimpleNamespace(
            plan_path=plan_path,
            objective_path=None,
            plan=parse_backlog_planner_output(
                {
                    "project_id": "demo",
                    "goal": "Goal.",
                    "roadmap_path": str(roadmap_path),
                    "planner": "deterministic",
                    "selected_epic_id": epic_id,
                    "warnings": [],
                    "epics": [
                        {
                            "epic_id": epic_id,
                            "title": f"Epic {epic_id}",
                            "objective": f"Do {epic_id}.",
                            "rationale": "Because.",
                            "priority": 1,
                            "source_refs": ["roadmap:1"],
                            "acceptance_criteria": ["It works."],
                            "suggested_release_id": f"demo-{epic_id}",
                        }
                    ],
                },
                project_id="demo",
            ),
        )

    plans = [_plan("epic-0001", first_plan_path), _plan("epic-0002", second_plan_path)]

    def fake_plan_backlog(**_kwargs):
        nonlocal plan_calls
        if plan_calls == 1:
            state = state_store.load()
            assert "epic-0001" in state.blocked_epics
            assert state.blocked_epic_records[0].status_reason == "release_failed"
        planned = plans[plan_calls]
        plan_calls += 1
        return planned

    def fake_run_objective(**kwargs):
        release_id = kwargs["objective_path"].stem
        release_dir = runs_dir / release_id
        release_dir.mkdir(parents=True, exist_ok=True)
        summary_path = release_dir / "release_summary.json"
        summary_path.write_text(json.dumps({"integration_branch": f"feature/{release_id}"}) + "\n", encoding="utf-8")
        if release_id == "demo-epic-0001":
            return SimpleNamespace(
                release_id=release_id,
                planning=SimpleNamespace(plan_path=release_dir / "contract_plan.json"),
                release=SimpleNamespace(
                    release_id=release_id,
                    summary_path=summary_path,
                    metrics_path=release_dir / "release_metrics.json",
                    budget_path=release_dir / "release_budget.json",
                    tuning_path=release_dir / "release_tuning.md",
                    decision="failed",
                    finalization_gate=None,
                    finalization=None,
                ),
            )
        return SimpleNamespace(
            release_id=release_id,
            planning=SimpleNamespace(plan_path=release_dir / "contract_plan.json"),
            release=SimpleNamespace(
                release_id=release_id,
                summary_path=summary_path,
                metrics_path=release_dir / "release_metrics.json",
                budget_path=release_dir / "release_budget.json",
                tuning_path=release_dir / "release_tuning.md",
                decision="accepted",
                finalization_gate={"allowed": True, "reason": "allowed", "decision": "accepted"},
                finalization=SimpleNamespace(error=None, failed_step=None, merged=True, pushed=True),
            ),
        )

    result = GovernorLoop(
        plan_backlog=fake_plan_backlog,
        run_objective=fake_run_objective,
        state_store=state_store,
    ).run_epics(
        project_id="demo",
        goal="Run two epics.",
        roadmap_path=roadmap_path,
        selected_epic_id=None,
        epic_count=2,
        config_dir=config_dir,
        contracts_dir=tmp_path / "contracts",
        runs_dir=runs_dir,
        objectives_dir=objectives_dir,
        mode="deterministic",
        planner_backend=None,
        objective_planner_backend=FakeObjectivePlannerBackend(),
        executor=FakeExecutor(),
        verification_timeout_seconds=60,
        allow_dirty=True,
        commit_on_accept=False,
        merge_on_accept=False,
        push_on_accept=False,
        release_finalize="none",
        integration_branch=None,
        stop_on_failure=False,
        execution_mode="sequential",
        debug_keep_artifacts=False,
        progress=None,
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    assert result.stop_reason == GovernorStopReason.REQUESTED_EPIC_COUNT_REACHED
    assert plan_calls == 2


def test_governor_loop_post_cycle_refresh_records_manual_completion_finalization_reference(tmp_path) -> None:
    from types import SimpleNamespace

    runs_dir = tmp_path / "runs"
    objectives_dir = tmp_path / "objectives"
    roadmap_path = tmp_path / "ROADMAP.md"
    roadmap_path.write_text("# Roadmap\n", encoding="utf-8")
    config_dir = _write_project_config(tmp_path)
    state_store = StateStore(tmp_path / "repo_state" / "demo" / "backlog_state.yaml")

    plan_path = runs_dir / "epic-0001" / "backlog_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("{}", encoding="utf-8")
    plan = parse_backlog_planner_output(
        {
            "project_id": "demo",
            "goal": "Goal.",
            "roadmap_path": str(roadmap_path),
            "planner": "deterministic",
            "selected_epic_id": "epic-0001",
            "warnings": [],
            "epics": [
                {
                    "epic_id": "epic-0001",
                    "title": "Epic 1",
                    "objective": "Do epic 1.",
                    "rationale": "Because.",
                    "priority": 1,
                    "source_refs": ["roadmap:1"],
                    "acceptance_criteria": ["It works."],
                    "suggested_release_id": "demo-epic-0001",
                }
            ],
        },
        project_id="demo",
    )

    def fake_plan_backlog(**_kwargs):
        return SimpleNamespace(plan_path=plan_path, plan=plan, objective_path=None)

    def fake_run_objective(**kwargs):
        release_id = kwargs["objective_path"].stem
        release_dir = runs_dir / release_id
        release_dir.mkdir(parents=True, exist_ok=True)
        summary_path = release_dir / "release_summary.json"
        summary_path.write_text(
            json.dumps({"integration_branch": "feature/demo-epic-0001", "integration_commit": "cafe1234"}) + "\n",
            encoding="utf-8",
        )
        return SimpleNamespace(
            release_id=release_id,
            planning=SimpleNamespace(plan_path=release_dir / "contract_plan.json"),
            release=SimpleNamespace(
                release_id=release_id,
                summary_path=summary_path,
                metrics_path=release_dir / "release_metrics.json",
                budget_path=release_dir / "release_budget.json",
                tuning_path=release_dir / "release_tuning.md",
                decision="accepted",
                finalization_gate={"allowed": True, "reason": "allowed", "decision": "accepted"},
                finalization=SimpleNamespace(error=None, failed_step=None, merged=False, pushed=False),
            ),
        )

    GovernorLoop(
        plan_backlog=fake_plan_backlog,
        run_objective=fake_run_objective,
        state_store=state_store,
    ).run_epics(
        project_id="demo",
        goal="Run one epic.",
        roadmap_path=roadmap_path,
        selected_epic_id=None,
        epic_count=1,
        config_dir=config_dir,
        contracts_dir=tmp_path / "contracts",
        runs_dir=runs_dir,
        objectives_dir=objectives_dir,
        mode="deterministic",
        planner_backend=None,
        objective_planner_backend=FakeObjectivePlannerBackend(),
        executor=FakeExecutor(),
        verification_timeout_seconds=60,
        allow_dirty=True,
        commit_on_accept=False,
        merge_on_accept=False,
        push_on_accept=False,
        release_finalize="none",
        integration_branch=None,
        stop_on_failure=True,
        execution_mode="sequential",
        debug_keep_artifacts=False,
        progress=None,
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    state = state_store.load()
    assert "epic-0001" in state.completed_epics
    completed = state.completed_epic_records[0]
    assert completed.status_reason == "accepted_manual_merge_or_completed"
    assert completed.finalization_outcome_references[0].branch == "feature/demo-epic-0001"
    assert completed.finalization_outcome_references[0].commit == "cafe1234"


def test_governor_loop_prevents_stale_duplicate_from_durable_state_with_existing_run_artifacts(tmp_path) -> None:
    from types import SimpleNamespace

    runs_dir = tmp_path / "runs"
    objectives_dir = tmp_path / "objectives"
    roadmap_path = tmp_path / "ROADMAP.md"
    roadmap_path.write_text("# Roadmap\n", encoding="utf-8")
    config_dir = _write_project_config(tmp_path)
    state_store = StateStore(tmp_path / "repo_state" / "demo" / "backlog_state.yaml")

    first_plan_path = runs_dir / "epic-0001" / "backlog_plan.json"
    first_plan_path.parent.mkdir(parents=True, exist_ok=True)
    first_plan_path.write_text("{}", encoding="utf-8")
    second_plan_path = runs_dir / "epic-0001-repeat" / "backlog_plan.json"
    second_plan_path.parent.mkdir(parents=True, exist_ok=True)
    second_plan_path.write_text("{}", encoding="utf-8")

    plan = parse_backlog_planner_output(
        {
            "project_id": "demo",
            "goal": "Goal.",
            "roadmap_path": str(roadmap_path),
            "planner": "deterministic",
            "selected_epic_id": "epic-0001",
            "warnings": [],
            "epics": [
                {
                    "epic_id": "epic-0001",
                    "title": "Epic 1",
                    "objective": "Do epic 1.",
                    "rationale": "Because.",
                    "priority": 1,
                    "source_refs": ["roadmap:1"],
                    "acceptance_criteria": ["It works."],
                    "suggested_release_id": "demo-epic-0001",
                }
            ],
        },
        project_id="demo",
    )
    plans = [
        SimpleNamespace(plan_path=first_plan_path, plan=plan, objective_path=None),
        SimpleNamespace(plan_path=second_plan_path, plan=plan, objective_path=None),
    ]
    plan_calls = 0
    run_calls = 0

    def fake_plan_backlog(**_kwargs):
        nonlocal plan_calls
        planned = plans[plan_calls]
        plan_calls += 1
        return planned

    def fake_run_objective(**kwargs):
        nonlocal run_calls
        run_calls += 1
        release_id = kwargs["objective_path"].stem
        release_dir = runs_dir / release_id
        release_dir.mkdir(parents=True, exist_ok=True)
        summary_path = release_dir / "release_summary.json"
        summary_path.write_text(
            json.dumps({"integration_branch": f"feature/{release_id}", "integration_commit": "deadbeef"}) + "\n",
            encoding="utf-8",
        )
        return SimpleNamespace(
            release_id=release_id,
            planning=SimpleNamespace(plan_path=release_dir / "contract_plan.json"),
            release=SimpleNamespace(
                release_id=release_id,
                summary_path=summary_path,
                metrics_path=release_dir / "release_metrics.json",
                budget_path=release_dir / "release_budget.json",
                tuning_path=release_dir / "release_tuning.md",
                decision="accepted",
                finalization_gate={"allowed": True, "reason": "allowed", "decision": "accepted"},
                finalization=SimpleNamespace(error=None, failed_step=None, merged=True, pushed=True),
            ),
        )

    result = GovernorLoop(
        plan_backlog=fake_plan_backlog,
        run_objective=fake_run_objective,
        state_store=state_store,
    ).run_epics(
        project_id="demo",
        goal="Run repeated epic.",
        roadmap_path=roadmap_path,
        selected_epic_id=None,
        epic_count=2,
        config_dir=config_dir,
        contracts_dir=tmp_path / "contracts",
        runs_dir=runs_dir,
        objectives_dir=objectives_dir,
        mode="deterministic",
        planner_backend=None,
        objective_planner_backend=FakeObjectivePlannerBackend(),
        executor=FakeExecutor(),
        verification_timeout_seconds=60,
        allow_dirty=True,
        commit_on_accept=False,
        merge_on_accept=False,
        push_on_accept=False,
        release_finalize="none",
        integration_branch=None,
        stop_on_failure=True,
        execution_mode="sequential",
        debug_keep_artifacts=False,
        progress=None,
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    assert run_calls == 1
    assert plan_calls == 2
    assert result.stop_reason == GovernorStopReason.NO_ACTIONABLE_WORK


def _write_yaml(path: Path, data: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _write_project_config(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    if not (repo / ".git").exists():
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "Test User")
        (repo / "README.md").write_text("# demo\n", encoding="utf-8")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-m", "initial")

    config_dir = tmp_path / "configs"
    config_dir.mkdir(exist_ok=True)
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {"type": "codex_cli", "model": "worker", "max_walltime_minutes": 5},
            "verification_profiles": {"default": {"commands": ["true"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )
    return config_dir
