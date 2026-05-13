from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from agentic_devloop.backlog import BacklogPlannerBackendResult, parse_backlog_planner_output, plan_backlog
from agentic_devloop.backlog import run_backlog
from agentic_devloop.governor import GovernorLoop
from agentic_devloop.models import ExecutorResult


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
    assert result.evidence_manifest.release_summary_path == result.release_summary_path
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
    assert result.evidence_manifest.release_summary_path == result.release_summary_path
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
        config_dir=tmp_path / "configs",
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


def _write_yaml(path: Path, data: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
