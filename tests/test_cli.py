from __future__ import annotations

from pathlib import Path

import pytest

from agentic_devloop import cli as cli_module
from agentic_devloop.cli import main
from agentic_devloop.models import ContractPlan, GeneratedContract, TaskContract
from agentic_devloop.planning import ContractPlanResult


def test_cli_help_exits_successfully(capsys) -> None:
    exit_code = main([])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "agent-loop" in captured.out


def test_init_prints_project_and_repo(capsys) -> None:
    exit_code = main(["init", "--project", "rust_rockfall", "--repo", "/tmp/rust_rockfall"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "project=rust_rockfall" in captured.out
    assert "repo=/tmp/rust_rockfall" in captured.out


def test_config_prints_project_config(capsys) -> None:
    exit_code = main(["config", "--project", "rust_rockfall"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert '"project_id": "rust_rockfall"' in captured.out


def test_run_task_command_is_registered(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["run-task", "--help"])

    captured = capsys.readouterr()

    assert error.value.code == 0
    assert "--contract" in captured.out
    assert "--push-on-accept" in captured.out


def test_run_release_command_is_registered(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["run-release", "--help"])

    captured = capsys.readouterr()

    assert error.value.code == 0
    assert "--release" in captured.out
    assert "--continue-on-failure" in captured.out
    assert "--execution-mode" in captured.out
    assert "--debug-keep-artifacts" in captured.out


def test_plan_release_command_is_registered(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["plan-release", "--help"])

    captured = capsys.readouterr()

    assert error.value.code == 0
    assert "--objective" in captured.out
    assert "--strong-model" in captured.out
    assert "--inspect-proposed-contracts" in captured.out
    assert "--write-contracts-dir" in captured.out


def test_plan_release_can_request_strong_planning_and_write_contracts(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    planned_contract = GeneratedContract(
        task_id="v1.0.0-0001",
        title="Draft API changes",
        objective="Implement bounded API support.",
        rationale="Covers one acceptance criterion.",
        suggested_contract=TaskContract.model_validate(
            {
                "task_id": "v1.0.0-0001",
                "release_id": "v1.0.0",
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
    result = ContractPlanResult(
        release_id="v1.0.0",
        plan_path=tmp_path / "runs" / "v1.0.0_plan" / "contract_plan.json",
        plan=ContractPlan(
            release_id="v1.0.0",
            planner="strong-model",
            generated_contracts=[planned_contract],
            warnings=["parsed"],
        ),
        written_contract_paths=[tmp_path / "drafts" / "v1.0.0-0001.yaml"],
    )
    seen_kwargs: dict[str, object] = {}

    def fake_plan_release_contracts(**kwargs):
        seen_kwargs.update(kwargs)
        return result

    monkeypatch.setattr(cli_module, "plan_release_contracts", fake_plan_release_contracts)

    exit_code = main(
        [
            "plan-release",
            "--objective",
            str(tmp_path / "objective.yaml"),
            "--strong-model",
            "--project",
            "demo",
            "--inspect-proposed-contracts",
            "--write-contracts-dir",
            str(tmp_path / "drafts"),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert seen_kwargs["mode"] == "strong-model"
    assert seen_kwargs["write_contracts_dir"] == tmp_path / "drafts"
    assert '"proposed_contracts"' in captured.out
    assert '"written_contract_paths"' in captured.out
    assert '"task_id": "v1.0.0-0001"' in captured.out
