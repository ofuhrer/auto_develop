from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agentic_devloop.models import (
    ContractNormalizationDecision,
    ContractNormalizationOutcome,
    ContractPlan,
    GeneratedContract,
    TaskContract,
)
from agentic_devloop.planner_backend import PlannerBackendResult
from agentic_devloop.planning import (
    PlannerNormalizationError,
    parse_planner_output,
    plan_release_contracts,
    validate_generated_contracts,
    write_generated_contracts,
)
from agentic_devloop.runtime_supervisor import RuntimeSupervisorApplierStopKind
from agentic_devloop.supervisor_decisions import (
    ModelOutputNormalizationDecision,
    ExecutionStrategyDecision,
    load_supervisor_decision_artifact,
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


def test_plan_release_contracts_records_state_review_snapshot_path(tmp_path) -> None:
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
    state_review_snapshot_path = tmp_path / "planning_artifacts" / "state_review_snapshot.json"
    state_review_snapshot_path.parent.mkdir(parents=True)
    state_review_snapshot_path.write_text("{}", encoding="utf-8")

    result = plan_release_contracts(
        objective_path=objective_path,
        contracts_dir=contracts_dir,
        runs_dir=tmp_path / "runs",
        state_review_snapshot_path=state_review_snapshot_path,
    )

    plan = json.loads(result.plan_path.read_text(encoding="utf-8"))
    assert plan["state_review_snapshot_path"] == str(state_review_snapshot_path)


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


def test_strong_model_plan_records_state_review_snapshot_path_with_backend_output(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    objective_path = tmp_path / "objective.yaml"
    _write_yaml(
        objective_path,
        {
            "release_id": "v0.3.4",
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
    state_review_snapshot_path = tmp_path / "planning_artifacts" / "state_review_snapshot.json"
    state_review_snapshot_path.parent.mkdir(parents=True)
    state_review_snapshot_path.write_text("{}", encoding="utf-8")

    class StubPlannerBackend:
        def generate(self, **_: object) -> dict[str, object]:
            return {
                "release_id": "v0.3.4",
                "planner": "strong-model",
                "generated_contracts": [],
                "warnings": [],
            }

    result = plan_release_contracts(
        objective_path=objective_path,
        contracts_dir=tmp_path / "contracts",
        runs_dir=tmp_path / "runs",
        mode="strong-model",
        project_id="demo",
        config_dir=config_dir,
        planner_backend=StubPlannerBackend(),
        state_review_snapshot_path=state_review_snapshot_path,
    )

    assert result.plan.state_review_snapshot_path == state_review_snapshot_path
    plan = json.loads(result.plan_path.read_text(encoding="utf-8"))
    assert plan["state_review_snapshot_path"] == str(state_review_snapshot_path)


def test_one_shot_strategy_skips_planner_backend_and_contract_writes(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    objective_path = tmp_path / "objective.yaml"
    _write_yaml(
        objective_path,
        {
            "release_id": "v1.0.0",
            "title": "Cohesive objective",
            "objective": "Implement a cohesive change.",
            "acceptance_criteria": ["One-shot input exists."],
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

    class ExplodingPlannerBackend:
        def generate(self, **_: object) -> dict[str, object]:
            raise AssertionError("planner backend must not be invoked for one-shot strategy")

    proposed_dir = tmp_path / "proposed-contracts"
    result = plan_release_contracts(
        objective_path=objective_path,
        contracts_dir=tmp_path / "contracts",
        runs_dir=tmp_path / "runs",
        write_contracts_dir=proposed_dir,
        mode="strong-model",
        project_id="demo",
        config_dir=config_dir,
        planner_backend=ExplodingPlannerBackend(),
        execution_strategy_inputs={
            "release_id": "v1.0.0",
            "task_ids": ["v1-0-0-0001"],
            "cohesive_scope": True,
        },
    )

    assert result.written_contract_paths == []
    assert result.execution_strategy_selection is not None
    assert result.execution_strategy_selection.selected_action.value == "one_shot"
    assert result.execution_strategy_selection_path is not None
    assert result.execution_strategy_selection_path.exists()
    assert result.supervisor_decision_path is not None
    assert result.supervisor_decision_path.exists()
    assert result.one_shot_execution_input_path is not None
    assert result.one_shot_execution_input_path.exists()


def test_execution_strategy_decision_uses_absolute_evidence_paths(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    objective_path = tmp_path / "objective.yaml"
    _write_yaml(
        objective_path,
        {
            "release_id": "v1.0.2",
            "title": "Cohesive objective",
            "objective": "Implement a cohesive change.",
            "acceptance_criteria": ["One-shot input exists."],
        },
    )
    snapshot_path = tmp_path / "runs" / "state_review_snapshot.json"
    snapshot_path.parent.mkdir()
    snapshot_path.write_text("{}\n", encoding="utf-8")
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
        planner_backend=None,
        state_review_snapshot_path=snapshot_path,
        execution_strategy_inputs={
            "release_id": "v1.0.2",
            "task_ids": ["v1-0-2-0001"],
            "cohesive_scope": True,
        },
    )

    assert result.supervisor_decision_path is not None
    decision = load_supervisor_decision_artifact(result.supervisor_decision_path)
    assert isinstance(decision, ExecutionStrategyDecision)
    assert decision.evidence_paths == [
        snapshot_path.resolve(),
        result.execution_strategy_selection_path.resolve(),
    ]


def test_decomposition_strategy_still_runs_planner_backend_and_writes_contracts(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    objective_path = tmp_path / "objective.yaml"
    _write_yaml(
        objective_path,
        {
            "release_id": "v1.0.1",
            "title": "Decomposed objective",
            "objective": "Implement independent tasks.",
            "acceptance_criteria": ["Contracts exist."],
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
    proposed_dir = tmp_path / "proposed-contracts"

    called = {"planner": False}

    class StubPlannerBackend:
        def generate(self, **_: object) -> dict[str, object]:
            called["planner"] = True
            return {
                "release_id": "v1.0.1",
                "planner": "strong-model",
                "generated_contracts": [
                    {
                        "task_id": "v1-0-1-0001",
                        "title": "Bounded task",
                        "objective": "Implement a bounded change.",
                        "rationale": "Decomposition requires at least one task.",
                        "suggested_contract": {
                            "task_id": "v1-0-1-0001",
                            "release_id": "v1.0.1",
                            "title": "Bounded task",
                            "task_type": "documentation",
                            "budget_class": "S",
                            "objective": "Write docs/bounded.md.",
                            "allowed_files": ["docs/bounded.md"],
                            "forbidden_changes": ["Do not edit source files."],
                            "required_evidence": ["git diff", "test output"],
                            "verification": {"commands": ["true"]},
                            "stop_conditions": ["Verification fails."],
                        },
                    }
                ],
                "warnings": [],
            }

    result = plan_release_contracts(
        objective_path=objective_path,
        contracts_dir=tmp_path / "contracts",
        runs_dir=tmp_path / "runs",
        write_contracts_dir=proposed_dir,
        mode="strong-model",
        project_id="demo",
        config_dir=config_dir,
        planner_backend=StubPlannerBackend(),
        execution_strategy_inputs={
            "release_id": "v1.0.1",
            "task_ids": ["v1-0-1-0001", "v1-0-1-0002"],
            "coupled_tasks": True,
        },
    )

    assert called["planner"] is True
    assert result.written_contract_paths == [proposed_dir / "v1-0-1-0001.yaml"]
    assert (proposed_dir / "v1-0-1-0001.yaml").exists()


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


def test_parse_planner_output_accepts_state_review_snapshot_path() -> None:
    plan = parse_planner_output(
        {
            "release_id": "v0.3.1",
            "planner": "strong-model",
            "generated_contracts": [],
            "warnings": [],
            "state_review_snapshot_path": "runs/demo/state_review_snapshot.json",
        },
        release_id="v0.3.1",
        planner="strong-model",
    )

    assert plan.state_review_snapshot_path == Path("runs/demo/state_review_snapshot.json")


def test_parse_planner_output_rejects_invalid_schema() -> None:
    with pytest.raises(PlannerNormalizationError, match="planner output did not match"):
        parse_planner_output(
            {
                "release_id": "v0.3.0",
                "planner": "strong-model",
                "generated_contracts": [{"task_id": "broken"}],
            },
            release_id="v0.3.0",
            planner="strong-model",
        )


def test_parse_planner_output_normalizes_supported_schema_drift_once() -> None:
    plan = parse_planner_output(
        {
            "release_id": "v0.3.2",
            "planner": "deterministic",
            "generated_contracts": [
                {
                    "task_id": "v0.3.2-0001",
                    "title": "Normalize",
                    "objective": "Normalize planner payload.",
                    "rationale": "Covers one criterion.",
                    "suggested_contract": {
                        "task_id": "v0.3.2-0001",
                        "release_id": "v0.3.2",
                        "title": "Normalize",
                        "task_type": "code_only",
                        "budget_class": "S",
                        "objective": "Normalize planner payload.",
                        "allowed_files": ["src/agentic_devloop/planning.py"],
                        "forbidden_changes": ["Do not touch release flow."],
                        "required_evidence": ["git diff"],
                        "verification": {"commands": ["true"]},
                        "stop_conditions": ["Stop when scope expands."],
                    },
                }
            ],
            "warnings": [],
        },
        release_id="v0.3.2",
        planner="strong-model",
    )

    assert plan.release_id == "v0.3.2"
    assert plan.planner == "strong-model"
    assert plan.generated_contracts[0].task_id == "v0.3.2-0001"


def test_parse_planner_output_normalizes_contract_wrapper_drift_before_strict_validation() -> None:
    config = _project_config(
        Path("/tmp"),
        verification_profiles={
            "default": {
                "commands": [
                    "PYTHONPATH=src /shared/.venv/bin/python -m pytest"
                ]
            }
        },
    )
    plan = parse_planner_output(
        {
            "release_id": "v0.3.21",
            "planner": "strong-model",
            "generated_contracts": [
                {
                    "task_id": "v0.3.21-0001",
                    "title": "Repair wrapper drift",
                    "objective": "Normalize a semantically useful planner contract.",
                    "rationale": "Planner emitted a useful but incomplete suggested contract.",
                    "suggested_contract": {
                        "allowed_files": ["src/agentic_devloop/planning.py"],
                        "forbidden_changes": ["Do not weaken validation."],
                        "requirements": ["This unknown key must remain rejected by strict validation."],
                        "verification": [".venv/bin/python -m pytest tests/test_planning.py"],
                        "stop_conditions": ["Stop if scope expands."],
                    },
                }
            ],
            "warnings": [],
        },
        release_id="v0.3.21",
        planner="strong-model",
        project_config=config,
    )

    contract = plan.generated_contracts[0].suggested_contract
    assert contract.task_id == "v0.3.21-0001"
    assert contract.release_id == "v0.3.21"
    assert contract.title == "Repair wrapper drift"
    assert contract.objective == "Normalize a semantically useful planner contract."
    assert contract.budget_class == "M"
    assert contract.required_evidence == ["git diff", "changed-files list"]
    assert contract.verification.commands == [
        "/shared/.venv/bin/python -m pytest tests/test_planning.py"
    ]
    assert any("planner_contract_payload_normalization=" in warning for warning in plan.warnings)
    assert any("planner_contract_normalization=" in warning for warning in plan.warnings)


def test_parse_planner_output_preserves_implementation_requirements_as_objective_detail() -> None:
    plan = parse_planner_output(
        {
            "release_id": "v0.3.22",
            "planner": "strong-model",
            "generated_contracts": [
                {
                    "task_id": "v0.3.22-0001",
                    "title": "State refresh artifact",
                    "objective": "Persist a governor state refresh summary.",
                    "rationale": "The planner emitted useful requirements outside the strict contract schema.",
                    "suggested_contract": {
                        "allowed_files": ["src/agentic_devloop/governor.py"],
                        "forbidden_changes": ["Do not change release finalization semantics."],
                        "implementation_requirements": [
                            "Add one typed artifact before epic selection.",
                            "Represent missing optional inputs explicitly.",
                        ],
                        "verification": [".venv/bin/python -m pytest tests/test_planning.py"],
                        "stop_conditions": ["Stop if scope expands."],
                    },
                }
            ],
            "warnings": [],
        },
        release_id="v0.3.22",
        planner="strong-model",
    )

    contract = plan.generated_contracts[0].suggested_contract
    assert contract.task_id == "v0.3.22-0001"
    assert "Implementation requirements:" in contract.objective
    assert "- Add one typed artifact before epic selection." in contract.objective
    assert contract.required_evidence == ["git diff", "changed-files list"]
    assert contract.verification.commands == [".venv/bin/python -m pytest tests/test_planning.py"]
    assert any("implementation_requirements" in warning for warning in plan.warnings)


def test_parse_planner_output_repairs_tail_brace_and_wrapper_depends_on() -> None:
    raw_output = json.dumps(
        {
            "release_id": "v0.3.23",
            "planner": "strong-model",
            "generated_contracts": [
                {
                    "task_id": "v0.3.23-0001",
                    "title": "Base task",
                    "objective": "Implement base task.",
                    "rationale": "Base task.",
                    "suggested_contract": {
                        "task_id": "v0.3.23-0001",
                        "release_id": "v0.3.23",
                        "title": "Base task",
                        "budget_class": "S",
                        "objective": "Implement base task.",
                        "allowed_files": ["src/agentic_devloop/planning.py"],
                        "forbidden_changes": ["Do not change release flow."],
                        "required_evidence": ["git diff"],
                        "verification": {"commands": ["true"]},
                        "stop_conditions": ["Stop if scope expands."],
                    },
                },
                {
                    "task_id": "v0.3.23-0002",
                    "title": "Dependent task",
                    "objective": "Implement dependent task.",
                    "rationale": "Dependent task.",
                    "suggested_contract": {
                        "task_id": "v0.3.23-0002",
                        "release_id": "v0.3.23",
                        "title": "Dependent task",
                        "budget_class": "S",
                        "objective": "Implement dependent task.",
                        "allowed_files": ["tests/test_planning.py"],
                        "forbidden_changes": ["Do not change release flow."],
                        "required_evidence": ["git diff"],
                        "verification": {"commands": ["true"]},
                        "stop_conditions": ["Stop if scope expands."],
                    },
                    "depends_on": ["v0.3.23-0001"],
                },
            ],
            "warnings": [],
        }
    )
    raw_output = raw_output.replace('}],"warnings"', '}}],"warnings"', 1)

    plan = parse_planner_output(raw_output, release_id="v0.3.23", planner="strong-model")

    dependent = plan.generated_contracts[1]
    assert dependent.suggested_contract.depends_on == ["v0.3.23-0001"]
    assert "depends_on" not in dependent.model_dump(mode="python")
    assert any("depends_on" in warning for warning in plan.warnings)


def test_parse_planner_output_normalizes_docs_and_tests_task_type() -> None:
    plan = parse_planner_output(
        {
            "release_id": "v0.3.24",
            "planner": "strong-model",
            "generated_contracts": [
                {
                    "task_id": "v0.3.24-0001",
                    "title": "Document and verify",
                    "objective": "Update docs and run final verification.",
                    "rationale": "Planner used a descriptive task type outside the strict enum.",
                    "suggested_contract": {
                        "task_id": "v0.3.24-0001",
                        "release_id": "v0.3.24",
                        "title": "Document and verify",
                        "task_type": "docs_and_tests",
                        "budget_class": "S",
                        "objective": "Update docs and run final verification.",
                        "allowed_files": ["docs/USER_GUIDE.md", "tests/test_cli.py"],
                        "forbidden_changes": ["Do not change runtime code."],
                        "required_evidence": ["git diff"],
                        "verification": {"commands": ["true"]},
                        "stop_conditions": ["Stop if documentation cannot remain truthful."],
                    },
                }
            ],
            "warnings": [],
        },
        release_id="v0.3.24",
        planner="strong-model",
    )

    contract = plan.generated_contracts[0].suggested_contract
    assert contract.task_type == "release_preparation"
    assert any("task_type" in warning for warning in plan.warnings)


def test_parse_planner_output_stops_with_structured_evidence_for_overbroad_allowed_files() -> None:
    with pytest.raises(PlannerNormalizationError) as exc:
        parse_planner_output(
            {
                "release_id": "v0.3.3",
                "planner": "strong-model",
                "generated_contracts": [
                    {
                        "task_id": "v0.3.3-0001",
                        "title": "Unsafe scope",
                        "objective": "Unsafe scope",
                        "rationale": "Unsafe scope",
                        "suggested_contract": {
                            "task_id": "v0.3.3-0001",
                            "release_id": "v0.3.3",
                            "title": "Unsafe scope",
                            "task_type": "code_only",
                            "budget_class": "S",
                            "objective": "Unsafe scope",
                            "allowed_files": ["**"],
                            "forbidden_changes": ["Do not touch release flow."],
                            "required_evidence": ["git diff"],
                            "verification": {"commands": ["true"]},
                            "stop_conditions": ["Stop when scope expands."],
                        },
                    }
                ],
                "warnings": [],
            },
            release_id="v0.3.3",
            planner="strong-model",
        )

    assert exc.value.stop_evidence.kind == RuntimeSupervisorApplierStopKind.BROADENS_ALLOWED_FILES
    assert "unsafe whole-repo" in exc.value.stop_evidence.reason


def test_parse_planner_output_stops_with_structured_evidence_for_budget_exceeded_generated_work(tmp_path) -> None:
    config = _project_config(
        tmp_path,
        verification_profiles={"default": {"commands": ["true"]}},
        max_changed_files_per_task=1,
    )
    with pytest.raises(PlannerNormalizationError) as exc:
        parse_planner_output(
            {
                "release_id": "v0.3.4",
                "planner": "strong-model",
                "generated_contracts": [
                    {
                        "task_id": "v0.3.4-0001",
                        "title": "Budget scope",
                        "objective": "Budget scope",
                        "rationale": "Budget scope",
                        "suggested_contract": {
                            "task_id": "v0.3.4-0001",
                            "release_id": "v0.3.4",
                            "title": "Budget scope",
                            "task_type": "code_only",
                            "budget_class": "S",
                            "objective": "Budget scope",
                            "allowed_files": ["src/one.py", "src/two.py"],
                            "forbidden_changes": ["Do not touch release flow."],
                            "required_evidence": ["git diff"],
                            "verification": {"profile": "default"},
                            "stop_conditions": ["Stop when scope expands."],
                        },
                    }
                ],
                "warnings": [],
            },
            release_id="v0.3.4",
            planner="strong-model",
            project_config=config,
        )

    assert exc.value.stop_evidence.kind == RuntimeSupervisorApplierStopKind.EXCEEDS_TASK_BUDGET
    assert "allowed_files count exceeds project budget" in exc.value.stop_evidence.reason


def test_parse_planner_output_normalizes_missing_diff_evidence_and_reruns_admission() -> None:
    plan = parse_planner_output(
        {
            "release_id": "v0.3.5",
            "planner": "strong-model",
            "generated_contracts": [
                {
                    "task_id": "v0.3.5-0001",
                    "title": "Repairable admission",
                    "objective": "Repair missing diff evidence.",
                    "rationale": "Repairable planner drift.",
                    "suggested_contract": {
                        "task_id": "v0.3.5-0001",
                        "release_id": "v0.3.5",
                        "title": "Repairable admission",
                        "task_type": "code_only",
                        "budget_class": "S",
                        "objective": "Repair missing diff evidence.",
                        "allowed_files": ["src/agentic_devloop/planning.py"],
                        "forbidden_changes": ["Do not touch release flow."],
                        "required_evidence": ["test output"],
                        "verification": {"commands": ["true"]},
                        "stop_conditions": ["Stop when scope expands."],
                    },
                }
            ],
            "warnings": [],
        },
        release_id="v0.3.5",
        planner="strong-model",
    )

    required = plan.generated_contracts[0].suggested_contract.required_evidence
    assert "git diff" in required
    assert "changed-files list" in required
    normalization_warnings = [warning for warning in plan.warnings if warning.startswith("planner_contract_normalization=")]
    assert len(normalization_warnings) == 1
    payload = json.loads(normalization_warnings[0].split("=", 1)[1])
    assert payload["decision"] == "normalized"
    assert payload["before_snapshot"]["contract"]["required_evidence"] == ["test output"]
    assert "git diff" in payload["after_snapshot"]["contract"]["required_evidence"]


def test_parse_planner_output_hard_stops_when_normalization_changes_guarded_semantics(monkeypatch) -> None:
    def _unsafe_normalization(*args, **kwargs):
        request = args[0]
        changed = request.before_snapshot.contract.model_copy(update={"allowed_files": ["src/unsafe.py"]})
        return ContractNormalizationOutcome(
            release_id=request.release_id,
            task_id=request.task_id,
            decision=ContractNormalizationDecision.NORMALIZED,
            rationale="Unsafe meaning-changing rewrite.",
            before_snapshot=request.before_snapshot,
            after_snapshot={"contract": changed},
            changed_fields=[],
            artifact_paths=request.artifact_paths,
        )

    monkeypatch.setattr("agentic_devloop.planning.normalize_contract_request", _unsafe_normalization)
    with pytest.raises(PlannerNormalizationError) as exc:
        parse_planner_output(
            {
                "release_id": "v0.3.6",
                "planner": "strong-model",
                "generated_contracts": [
                    {
                        "task_id": "v0.3.6-0001",
                        "title": "Unsafe normalization",
                        "objective": "Repair missing diff evidence.",
                        "rationale": "Repairable planner drift.",
                        "suggested_contract": {
                            "task_id": "v0.3.6-0001",
                            "release_id": "v0.3.6",
                            "title": "Unsafe normalization",
                            "task_type": "code_only",
                            "budget_class": "S",
                            "objective": "Repair missing diff evidence.",
                            "allowed_files": ["src/agentic_devloop/planning.py"],
                            "forbidden_changes": ["Do not touch release flow."],
                            "required_evidence": ["test output"],
                            "verification": {"commands": ["true"]},
                            "stop_conditions": ["Stop when scope expands."],
                        },
                    }
                ],
                "warnings": [],
            },
            release_id="v0.3.6",
            planner="strong-model",
        )

    assert exc.value.stop_evidence.kind == RuntimeSupervisorApplierStopKind.BYPASSES_HARD_GATE
    assert "allowed_files" in exc.value.stop_evidence.reason


def test_strong_model_plan_persists_model_output_normalization_decision_with_validation_errors(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    objective_path = tmp_path / "objective.yaml"
    _write_yaml(
        objective_path,
        {
            "release_id": "v0.4.1",
            "title": "Normalization evidence",
            "objective": "Persist model-output normalization decisions.",
            "acceptance_criteria": ["Normalization artifact is persisted."],
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

    class DriftPlannerBackend:
        def generate(self, **_: object) -> PlannerBackendResult:
            output_dir = tmp_path / "planner_backend"
            output_dir.mkdir(parents=True, exist_ok=True)
            stdout_path = output_dir / "planner_stdout.log"
            stderr_path = output_dir / "planner_stderr.log"
            metadata_path = output_dir / "planner_metadata.json"
            stdout_path.write_text("planner output", encoding="utf-8")
            stderr_path.write_text("", encoding="utf-8")
            metadata_path.write_text("{}", encoding="utf-8")
            return PlannerBackendResult(
                raw_output={
                    "release_id": "v0.4.1",
                    "planner": "strong-model",
                    "generated_contracts": [
                        {
                            "task_id": "v0.4.1-0001",
                            "title": "Wrapper drift",
                            "objective": "Normalize schema drift.",
                            "rationale": "Useful output with alias keys.",
                                "suggested_contract": {
                                    "allowedFiles": ["src/agentic_devloop/planning.py"],
                                    "forbiddenChanges": ["Do not touch release flow."],
                                    "verificationCommands": ["true"],
                                    "stopConditions": ["Stop when scope expands."],
                                },
                            }
                        ],
                    "warnings": [],
                },
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                metadata_path=metadata_path,
            )

    result = plan_release_contracts(
        objective_path=objective_path,
        contracts_dir=tmp_path / "contracts",
        runs_dir=tmp_path / "runs",
        mode="strong-model",
        project_id="demo",
        config_dir=config_dir,
        planner_backend=DriftPlannerBackend(),
    )

    warning = next(
        item for item in result.plan.warnings if item.startswith("model_output_normalization_decision_path=")
    )
    decision_path = Path(warning.split("=", 1)[1])
    decision = load_supervisor_decision_artifact(decision_path)
    assert isinstance(decision, ModelOutputNormalizationDecision)
    assert decision.validation_errors
    assert any(error.field.startswith("generated_contracts.0.suggested_contract") for error in decision.validation_errors)
    assert decision.normalized_artifact_path is not None


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


def test_validate_generated_contracts_rejects_missing_diff_evidence() -> None:
    plan = _contract_plan_with_allowed_files(
        release_id="v0.5.0",
        task_id="v0.5.0-0001",
        contract_release_id="v0.5.0",
        allowed_files=["src/agentic_devloop/planning.py"],
        required_evidence=["test output"],
    )

    with pytest.raises(ValueError, match="diff evidence"):
        validate_generated_contracts(plan)


def test_validate_generated_contracts_rejects_weak_stop_conditions() -> None:
    plan = _contract_plan_with_allowed_files(
        release_id="v0.5.0",
        task_id="v0.5.0-0001",
        contract_release_id="v0.5.0",
        allowed_files=["src/agentic_devloop/planning.py"],
        stop_conditions=["Ask a human."],
    )

    with pytest.raises(ValueError, match="scope or verification stop"):
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


def test_strong_model_plan_persists_planner_backend_evidence_paths(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    objective_path = tmp_path / "objective.yaml"
    _write_yaml(
        objective_path,
        {
            "release_id": "v0.4.1",
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
    stdout_path = tmp_path / "planner_stdout.log"
    stderr_path = tmp_path / "planner_stderr.log"
    metadata_path = tmp_path / "planner_metadata.json"
    stdout_path.write_text("planner stdout\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    metadata_path.write_text("{}\n", encoding="utf-8")

    class EvidencePlannerBackend:
        def generate(self, *, prompt: str, objective, existing_contracts, model):
            return PlannerBackendResult(
                raw_output={
                    "release_id": "v0.4.1",
                    "planner": "deterministic",
                    "generated_contracts": [
                        {
                            "task_id": "v0.4.1-0001",
                            "title": "Draft API changes",
                            "objective": "Implement bounded API support.",
                            "rationale": "Covers one acceptance criterion.",
                            "suggested_contract": {
                                "task_id": "v0.4.1-0001",
                                "release_id": "v0.4.1",
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
                    "warnings": [],
                },
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                metadata_path=metadata_path,
            )

    result = plan_release_contracts(
        objective_path=objective_path,
        contracts_dir=tmp_path / "contracts",
        runs_dir=tmp_path / "runs",
        mode="strong-model",
        project_id="demo",
        config_dir=config_dir,
        planner_backend=EvidencePlannerBackend(),
    )
    plan_json = result.plan_path.read_text(encoding="utf-8")

    assert result.plan.planner_stdout_path == stdout_path
    assert result.plan.planner_stderr_path == stderr_path
    assert result.plan.planner_metadata_path == metadata_path
    assert '"planner_metadata_path":' in plan_json


def test_plan_release_retargets_planner_backend_to_plan_directory(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    objective_path = tmp_path / "objective.yaml"
    _write_yaml(
        objective_path,
        {
            "release_id": "v0.4.2",
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

    class RetargetingPlannerBackend:
        def __init__(self, output_dir: Path | None = None) -> None:
            self.output_dir = output_dir

        def with_output_dir(self, output_dir: Path):
            return RetargetingPlannerBackend(output_dir)

        def generate(self, *, prompt: str, objective, existing_contracts, model):
            assert self.output_dir is not None
            stdout_path = self.output_dir / "planner_stdout.log"
            stderr_path = self.output_dir / "planner_stderr.log"
            metadata_path = self.output_dir / "planner_metadata.json"
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_path.write_text("planner stdout\n", encoding="utf-8")
            stderr_path.write_text("", encoding="utf-8")
            metadata_path.write_text("{}\n", encoding="utf-8")
            return PlannerBackendResult(
                raw_output={
                    "release_id": "v0.4.2",
                    "planner": "deterministic",
                    "generated_contracts": [],
                    "warnings": [],
                },
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                metadata_path=metadata_path,
            )

    result = plan_release_contracts(
        objective_path=objective_path,
        contracts_dir=tmp_path / "contracts",
        runs_dir=tmp_path / "runs",
        mode="strong-model",
        project_id="demo",
        config_dir=config_dir,
        planner_backend=RetargetingPlannerBackend(),
    )

    assert result.plan.planner_metadata_path is not None
    assert result.plan.planner_metadata_path.parent == result.plan_path.parent / "planner_backend"


def test_write_generated_contracts_rejects_unknown_profile_when_configured(tmp_path) -> None:
    config = _project_config(tmp_path, verification_profiles={"default": {"commands": ["true"]}})
    plan = ContractPlan(
        release_id="v0.7.0",
        planner="strong-model",
        generated_contracts=[
            GeneratedContract(
                task_id="v0.7.0-0001",
                title="Draft API changes",
                objective="Implement bounded API support.",
                rationale="Covers one acceptance criterion.",
                suggested_contract=TaskContract.model_validate(
                    {
                        "task_id": "v0.7.0-0001",
                        "release_id": "v0.7.0",
                        "title": "Draft API changes",
                        "task_type": "code_only",
                        "budget_class": "M",
                        "objective": "Implement bounded API support.",
                        "allowed_files": ["src/agentic_devloop/planning.py"],
                        "forbidden_changes": ["Do not touch release contracts."],
                        "required_evidence": ["plan diff"],
                        "verification": {"profile": "missing"},
                        "stop_conditions": ["Scope expands beyond the allowed file."],
                    }
                ),
            )
        ],
    )

    with pytest.raises(ValueError, match="unknown verification profile"):
        write_generated_contracts(plan, tmp_path / "contracts", project_config=config)


def test_plan_release_rejects_unknown_profile_before_writing_plan(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    objective_path = tmp_path / "objective.yaml"
    _write_yaml(
        objective_path,
        {
            "release_id": "v0.8.0",
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

    class InvalidPlannerBackend:
        def generate(self, *, prompt: str, objective, existing_contracts, model):
            return {
                "release_id": "v0.8.0",
                "planner": "strong-model",
                "generated_contracts": [
                    {
                        "task_id": "v0.8.0-0001",
                        "title": "Draft API changes",
                        "objective": "Implement bounded API support.",
                        "rationale": "Covers one acceptance criterion.",
                        "suggested_contract": {
                            "task_id": "v0.8.0-0001",
                            "release_id": "v0.8.0",
                            "title": "Draft API changes",
                            "task_type": "code_only",
                            "budget_class": "M",
                            "objective": "Implement bounded API support.",
                            "allowed_files": ["src/agentic_devloop/planning.py"],
                            "forbidden_changes": ["Do not touch release contracts."],
                            "required_evidence": ["plan diff"],
                            "verification": {"profile": "missing"},
                            "stop_conditions": ["Scope expands beyond the allowed file."],
                        },
                    }
                ],
                "warnings": [],
            }

    with pytest.raises(ValueError, match="unknown verification profile"):
        plan_release_contracts(
            objective_path=objective_path,
            contracts_dir=tmp_path / "contracts",
            runs_dir=tmp_path / "runs",
            mode="strong-model",
            project_id="demo",
            config_dir=config_dir,
            planner_backend=InvalidPlannerBackend(),
        )


def test_write_generated_contracts_rejects_allowed_file_count_over_budget(tmp_path) -> None:
    config = _project_config(
        tmp_path,
        max_changed_files_per_task=1,
        verification_profiles={"default": {"commands": ["true"]}},
    )
    plan = _contract_plan_with_allowed_files(
        release_id="v0.7.1",
        task_id="v0.7.1-0001",
        contract_release_id="v0.7.1",
        allowed_files=["src/one.py", "src/two.py"],
    )

    with pytest.raises(ValueError, match="allowed_files count exceeds"):
        write_generated_contracts(plan, tmp_path / "contracts", project_config=config)


def _contract_plan_with_allowed_files(
    *,
    release_id: str,
    task_id: str,
    contract_release_id: str,
    allowed_files: list[str],
    required_evidence: list[str] | None = None,
    stop_conditions: list[str] | None = None,
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
                        "required_evidence": required_evidence or ["plan diff"],
                        "verification": {"commands": ["true"]},
                        "stop_conditions": stop_conditions or ["Scope expands beyond the allowed file."],
                    }
                ),
            )
        ],
    )


def _project_config(
    tmp_path,
    *,
    verification_profiles: dict,
    max_changed_files_per_task: int = 8,
):
    from agentic_devloop.models import ProjectConfig

    return ProjectConfig.model_validate(
        {
            "project_id": "demo",
            "repo_path": str(tmp_path / "repo"),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {"type": "codex_cli", "model": "worker", "max_walltime_minutes": 5},
            "verification_profiles": verification_profiles,
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 1,
                "max_changed_files_per_task": max_changed_files_per_task,
                "max_diff_lines_per_task": 600,
            },
        }
    )


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
