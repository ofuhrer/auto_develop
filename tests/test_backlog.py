from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import yaml

from agentic_devloop.backlog import BacklogPlannerBackendResult, parse_backlog_planner_output, plan_backlog
from agentic_devloop.backlog import run_backlog
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
    assert result.objective_path == existing_objective
    assert result.generated_objective_path is None
    assert result.objective.release_id == "demo-agent-selected-governor"
    assert result.contract_plan_path is not None
    assert result.contract_plan_path.exists()
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


def _write_yaml(path: Path, data: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
