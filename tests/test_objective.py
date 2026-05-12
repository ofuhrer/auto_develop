from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from agentic_devloop.models import ExecutorResult
from agentic_devloop.objective import run_objective


class FakePlannerBackend:
    def generate(self, *, prompt: str, objective, existing_contracts, model):
        assert "Strong Release Planning Prompt" in prompt
        assert objective.release_id == "v1.2.0"
        assert existing_contracts == []
        assert model == "planner"
        return {
            "release_id": "v1.2.0",
            "planner": "strong-model",
            "generated_contracts": [
                {
                    "task_id": "objective-0001",
                    "title": "Create objective docs",
                    "objective": "Create one objective evidence document.",
                    "rationale": "Covers the objective with one bounded docs task.",
                    "suggested_contract": {
                        "task_id": "objective-0001",
                        "release_id": "v1.2.0",
                        "title": "Create objective docs",
                        "task_type": "documentation",
                        "budget_class": "S",
                        "objective": "Create docs/objective.md.",
                        "allowed_files": ["docs/objective.md"],
                        "forbidden_changes": ["Do not edit source files."],
                        "required_evidence": ["git diff", "test output"],
                        "verification": {"profile": "default"},
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


def test_run_objective_plans_writes_contracts_and_runs_release(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

    objective_path = tmp_path / "objective.yaml"
    _write_yaml(
        objective_path,
        {
            "release_id": "v1.2.0",
            "title": "Objective release",
            "objective": "Create one docs artifact.",
            "acceptance_criteria": ["docs/objective.md exists"],
        },
    )
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
                "max_strong_model_calls_per_release": 2,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )

    result = run_objective(
        project_id="demo",
        objective_path=objective_path,
        config_dir=config_dir,
        contracts_dir=tmp_path / "contracts",
        runs_dir=tmp_path / "runs",
        planning_mode="strong-model",
        planner_backend=FakePlannerBackend(),
        executor=FakeExecutor(),
    )

    assert result.release_id == "v1.2.0"
    assert result.planning.written_contract_paths == [tmp_path / "contracts" / "objective-0001.yaml"]
    assert result.release.decision == "accepted"
    assert result.release.task_results[0].decision.task_id == "objective-0001"


def _git(repo, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
