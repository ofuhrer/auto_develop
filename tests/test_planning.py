from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agentic_devloop.models import ContractPlan, GeneratedContract, TaskContract
from agentic_devloop.planning import (
    parse_planner_output,
    plan_release_contracts,
    validate_generated_contracts,
    write_generated_contracts,
)
from agentic_devloop.yaml_io import load_yaml_model


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


def test_plan_release_contracts_writes_validated_proposed_contracts(tmp_path) -> None:
    objective_path = tmp_path / "objective.yaml"
    _write_yaml(
        objective_path,
        {
            "release_id": "v0.2.1",
            "title": "Small release",
            "objective": "Ship one bounded increment.",
            "acceptance_criteria": ["Contract evidence exists."],
        },
    )
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    proposed_dir = tmp_path / "proposed-contracts"

    result = plan_release_contracts(
        objective_path=objective_path,
        contracts_dir=contracts_dir,
        runs_dir=tmp_path / "runs",
        write_contracts_dir=proposed_dir,
    )

    expected_contract_path = proposed_dir / "v0-2-1-0001.yaml"
    assert result.written_contract_paths == [expected_contract_path]
    written_contract = load_yaml_model(expected_contract_path, TaskContract)
    assert written_contract.task_id == "v0-2-1-0001"
    assert written_contract.release_id == "v0.2.1"
    assert written_contract.task_type == "release_preparation"


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


def test_parse_planner_output_validates_nested_task_contracts() -> None:
    plan = parse_planner_output(
        json.dumps(
            {
                "release_id": "v0.3.0",
                "planner": "deterministic",
                "generated_contracts": [
                    {
                        "task_id": "v0.3.0-0001",
                        "title": "Draft API changes",
                        "objective": "Implement bounded API support.",
                        "rationale": "Covers one acceptance criterion.",
                        "suggested_contract": {
                            "task_id": "v0.3.0-0001",
                            "release_id": "v0.3.0",
                            "title": "Draft API changes",
                            "task_type": "code_only",
                            "budget_class": "M",
                            "objective": "Implement bounded API support.",
                            "allowed_files": ["src/agentic_devloop/planning.py"],
                            "forbidden_changes": ["Do not touch release contracts."],
                            "required_evidence": ["plan diff"],
                            "verification": {"commands": ["true"]},
                            "stop_conditions": ["Scope expands beyond the allowed file."],
                        },
                    }
                ],
                "warnings": ["parsed"],
            }
        ),
        release_id="v0.3.0",
        planner="strong-model",
    )

    assert plan.planner == "strong-model"
    assert plan.generated_contracts[0].suggested_contract.task_id == "v0.3.0-0001"
    assert plan.generated_contracts[0].suggested_contract.verification.commands == ["true"]


def test_parse_planner_output_accepts_fenced_json() -> None:
    plan = parse_planner_output(
        """```json
{
  "release_id": "v0.3.1",
  "planner": "strong-model",
  "generated_contracts": [],
  "warnings": []
}
```""",
        release_id="v0.3.1",
        planner="strong-model",
    )

    assert plan.release_id == "v0.3.1"


def test_parse_planner_output_rejects_invalid_schema() -> None:
    with pytest.raises(ValueError, match="planner output did not match"):
        parse_planner_output(
            {
                "release_id": "v0.3.0",
                "planner": "strong-model",
                "generated_contracts": [{"task_id": "broken"}],
            },
            release_id="v0.3.0",
            planner="strong-model",
        )


def test_validate_generated_contracts_rejects_task_id_mismatch() -> None:
    plan = ContractPlan(
        release_id="v0.5.0",
        planner="strong-model",
        generated_contracts=[
            GeneratedContract(
                task_id="v0.5.0-0001",
                title="Draft API changes",
                objective="Implement bounded API support.",
                rationale="Covers one acceptance criterion.",
                suggested_contract=TaskContract.model_validate(
                    {
                        "task_id": "v0.5.0-9999",
                        "release_id": "v0.5.0",
                        "title": "Draft API changes",
                        "task_type": "code_only",
                        "budget_class": "M",
                        "objective": "Implement bounded API support.",
                        "allowed_files": ["src/agentic_devloop/planning.py"],
                        "forbidden_changes": ["Do not touch release contracts."],
                        "required_evidence": ["plan diff"],
                        "verification": {"commands": ["true"]},
                        "stop_conditions": ["Scope expands beyond the allowed file."],
                    }
                ),
            )
        ],
    )

    with pytest.raises(ValueError, match="did not match suggested contract task_id"):
        validate_generated_contracts(plan)


def test_validate_generated_contracts_rejects_release_mismatch() -> None:
    plan = _contract_plan_with_allowed_files(
        release_id="v0.5.0",
        task_id="v0.5.0-0001",
        contract_release_id="v0.5.1",
        allowed_files=["src/agentic_devloop/planning.py"],
    )

    with pytest.raises(ValueError, match="did not match plan release_id"):
        validate_generated_contracts(plan)


def test_validate_generated_contracts_rejects_whole_repo_scope() -> None:
    plan = _contract_plan_with_allowed_files(
        release_id="v0.5.0",
        task_id="v0.5.0-0001",
        contract_release_id="v0.5.0",
        allowed_files=["**"],
    )

    with pytest.raises(ValueError, match="unsafe whole-repo"):
        validate_generated_contracts(plan)


def test_write_generated_contracts_refuses_to_overwrite_existing_contract(tmp_path) -> None:
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    existing_path = contracts_dir / "v0.6.0-0001.yaml"
    existing_path.write_text("task_id: existing\n", encoding="utf-8")
    plan = ContractPlan(
        release_id="v0.6.0",
        planner="deterministic",
        generated_contracts=[
            GeneratedContract(
                task_id="v0.6.0-0001",
                title="Prepare contract set",
                objective="Create bounded implementation contracts.",
                rationale="Start with a planning-only task.",
                suggested_contract=TaskContract.model_validate(
                    {
                        "task_id": "v0.6.0-0001",
                        "release_id": "v0.6.0",
                        "title": "Prepare contract set",
                        "task_type": "release_preparation",
                        "budget_class": "L",
                        "objective": "Create bounded implementation contracts.",
                        "allowed_files": ["contracts/**"],
                        "forbidden_changes": ["Do not modify source code while planning contracts."],
                        "required_evidence": ["contract diff"],
                        "verification": {"profile": "documentation"},
                        "stop_conditions": ["Generated contracts cannot be bounded to allowed files."],
                    }
                ),
            )
        ],
    )

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        write_generated_contracts(plan, contracts_dir)
    assert existing_path.read_text(encoding="utf-8") == "task_id: existing\n"


def test_strong_model_plan_uses_backend_seam_to_parse_structured_output(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    objective_path = tmp_path / "objective.yaml"
    _write_yaml(
        objective_path,
        {
            "release_id": "v0.4.0",
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

    class FakePlannerBackend:
        def __init__(self) -> None:
            self.prompt: str | None = None

        def generate(self, *, prompt: str, objective, existing_contracts, model):
            self.prompt = prompt
            assert objective.release_id == "v0.4.0"
            assert not existing_contracts
            assert model == "gpt-5.5"
            return {
                "release_id": "v0.4.0",
                "planner": "deterministic",
                "generated_contracts": [
                    {
                        "task_id": "v0.4.0-0001",
                        "title": "Draft API changes",
                        "objective": "Implement bounded API support.",
                        "rationale": "Covers one acceptance criterion.",
                        "suggested_contract": {
                            "task_id": "v0.4.0-0001",
                            "release_id": "v0.4.0",
                            "title": "Draft API changes",
                            "task_type": "code_only",
                            "budget_class": "M",
                            "objective": "Implement bounded API support.",
                            "allowed_files": ["src/agentic_devloop/planning.py"],
                            "forbidden_changes": ["Do not touch release contracts."],
                            "required_evidence": ["plan diff"],
                            "verification": {"commands": ["true"]},
                            "stop_conditions": ["Scope expands beyond the allowed file."],
                        },
                    }
                ],
                "warnings": ["backend output parsed"],
            }

    backend = FakePlannerBackend()

    result = plan_release_contracts(
        objective_path=objective_path,
        contracts_dir=tmp_path / "contracts",
        runs_dir=tmp_path / "runs",
        mode="strong-model",
        project_id="demo",
        config_dir=config_dir,
        planner_backend=backend,
    )

    assert backend.prompt is not None
    assert "Strong Release Planning Prompt" in backend.prompt
    assert result.plan.planner == "strong-model"
    assert result.plan.generated_contracts[0].suggested_contract.release_id == "v0.4.0"
    assert result.plan.budget_ledger_path is not None
    assert result.plan.planner_prompt_path is not None
    assert result.plan.planner_prompt_path.exists()


def _contract_plan_with_allowed_files(
    *,
    release_id: str,
    task_id: str,
    contract_release_id: str,
    allowed_files: list[str],
) -> ContractPlan:
    return ContractPlan(
        release_id=release_id,
        planner="strong-model",
        generated_contracts=[
            GeneratedContract(
                task_id=task_id,
                title="Draft API changes",
                objective="Implement bounded API support.",
                rationale="Covers one acceptance criterion.",
                suggested_contract=TaskContract.model_validate(
                    {
                        "task_id": task_id,
                        "release_id": contract_release_id,
                        "title": "Draft API changes",
                        "task_type": "code_only",
                        "budget_class": "M",
                        "objective": "Implement bounded API support.",
                        "allowed_files": allowed_files,
                        "forbidden_changes": ["Do not touch release contracts."],
                        "required_evidence": ["plan diff"],
                        "verification": {"commands": ["true"]},
                        "stop_conditions": ["Scope expands beyond the allowed file."],
                    }
                ),
            )
        ],
    )


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
