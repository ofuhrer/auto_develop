from __future__ import annotations

import json
from pathlib import Path

import yaml

from agentic_devloop.planning import plan_release_contracts


def test_plan_release_contracts_writes_conservative_draft_when_no_contracts_exist(tmp_path) -> None:
    objective_path = tmp_path / "objective.yaml"
    _write_yaml(
        objective_path,
        {
            "release_id": "v0.2.0",
            "title": "Small release",
            "objective": "Ship one bounded increment.",
            "acceptance_criteria": ["Contract evidence exists."],
        },
    )
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()

    result = plan_release_contracts(
        objective_path=objective_path,
        contracts_dir=contracts_dir,
        runs_dir=tmp_path / "runs",
    )

    plan = json.loads(result.plan_path.read_text(encoding="utf-8"))
    assert plan["release_id"] == "v0.2.0"
    assert plan["planner"] == "deterministic"
    assert plan["generated_contracts"][0]["suggested_contract"]["task_type"] == "release_preparation"
    assert plan["warnings"]


def test_strong_model_plan_reserves_budget_and_writes_prompt(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    objective_path = tmp_path / "objective.yaml"
    _write_yaml(
        objective_path,
        {
            "release_id": "v0.3.0",
            "title": "Small release",
            "objective": "Ship one bounded increment.",
            "acceptance_criteria": ["Contract evidence exists."],
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
                "planner": {"type": "codex_cli", "model": "gpt-5.5", "max_walltime_minutes": 5}
            },
            "model_routing": {"default_role": "planner"},
            "verification_profiles": {"default": {"commands": ["true"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 1,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )

    result = plan_release_contracts(
        objective_path=objective_path,
        contracts_dir=tmp_path / "contracts",
        runs_dir=tmp_path / "runs",
        mode="strong-model",
        project_id="demo",
        config_dir=config_dir,
    )

    assert result.plan.budget_ledger_path is not None
    assert result.plan.budget_ledger_path.exists()
    assert result.plan.planner_prompt_path is not None
    assert result.plan.planner_prompt_path.exists()


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
