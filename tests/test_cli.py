from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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


def test_doctor_command_is_registered(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["doctor", "--help"])

    captured = capsys.readouterr()

    assert error.value.code == 0
    assert "--project" in captured.out
    assert "--release" in captured.out


def test_doctor_command_outputs_report(monkeypatch, capsys) -> None:
    seen_kwargs: dict[str, object] = {}

    class Report:
        def to_dict(self) -> dict[str, object]:
            return {
                "project_id": "demo",
                "diagnostics": [{"check": "git", "severity": "warning", "message": "example"}],
            }

    def fake_run_doctor(**kwargs):
        seen_kwargs.update(kwargs)
        return Report()

    monkeypatch.setattr(cli_module, "run_doctor", fake_run_doctor)

    exit_code = main(["doctor", "--project", "demo", "--release", "v1.0.0"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert seen_kwargs["project_id"] == "demo"
    assert seen_kwargs["release_id"] == "v1.0.0"
    assert '"project_id": "demo"' in captured.out
    assert '"severity": "warning"' in captured.out


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


def test_run_release_command_outputs_budget_artifact_paths(monkeypatch, capsys, tmp_path) -> None:
    result = SimpleNamespace(
        release_id="v1.0.0",
        run_id="run-1",
        summary_path=tmp_path / "runs" / "release_summary.json",
        log_path=tmp_path / "runs" / "release.log",
        review_path=tmp_path / "runs" / "release_review.md",
        metrics_path=tmp_path / "runs" / "release_metrics.json",
        budget_path=tmp_path / "runs" / "release_budget.json",
        tuning_path=tmp_path / "runs" / "release_tuning.md",
        integration_branch="feature/v1.0.0",
        decision="accepted",
        task_results=[],
    )

    monkeypatch.setattr(cli_module, "run_release", lambda **kwargs: result)

    exit_code = main(["run-release", "--project", "demo", "--release", "v1.0.0"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert '"budget_path":' in captured.out
    assert "release_budget.json" in captured.out
    assert '"tuning_path":' in captured.out
    assert "release_tuning.md" in captured.out


def test_plan_release_command_is_registered(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["plan-release", "--help"])

    captured = capsys.readouterr()

    assert error.value.code == 0
    assert "--objective" in captured.out
    assert "--strong-model" in captured.out
    assert "--inspect-proposed-contracts" in captured.out
    assert "--write-contracts-dir" in captured.out
    assert "--execute-planner" in captured.out


def test_run_objective_command_is_registered(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["run-objective", "--help"])

    captured = capsys.readouterr()

    assert error.value.code == 0
    assert "--objective" in captured.out
    assert "--execute-planner" in captured.out
    assert "--merge-on-accept" in captured.out


def test_plan_backlog_command_is_registered(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["plan-backlog", "--help"])

    captured = capsys.readouterr()

    assert error.value.code == 0
    assert "--goal" in captured.out
    assert "--roadmap" in captured.out
    assert "--write-objective" in captured.out
    assert "--execute-planner" in captured.out


def test_run_backlog_command_is_registered(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["run-backlog", "--help"])

    captured = capsys.readouterr()

    assert error.value.code == 0
    assert "--epic-id" in captured.out
    assert "--goal" in captured.out
    assert "--execute-planner" in captured.out
    assert "--release-finalize" in captured.out


def test_plan_backlog_command_outputs_selected_epic(monkeypatch, capsys, tmp_path) -> None:
    from agentic_devloop.models import BacklogEpic, BacklogPlan

    plan = BacklogPlan(
        project_id="demo",
        goal="Ship autonomous roadmap governance.",
        roadmap_path=tmp_path / "ROADMAP.md",
        selected_epic_id="epic-0001",
        objective_path=tmp_path / "objectives" / "demo.yaml",
        epics=[
            BacklogEpic(
                epic_id="epic-0001",
                title="Add backlog planner",
                objective="Add backlog planner.",
                rationale="Advances the goal.",
                priority=1,
                source_refs=["roadmap:1"],
                acceptance_criteria=["Objective exists."],
                suggested_release_id="demo-20260512-add-backlog-planner",
            )
        ],
    )
    result = SimpleNamespace(
        plan_path=tmp_path / "runs" / "backlog_plan.json",
        objective_path=tmp_path / "objectives" / "demo.yaml",
        plan=plan,
    )

    def fake_plan_backlog(**kwargs):
        assert kwargs["mode"] == "strong-model"
        assert kwargs["planner_backend"] is not None
        return result

    monkeypatch.setattr(cli_module, "plan_backlog", fake_plan_backlog)
    monkeypatch.setattr(cli_module, "_codex_backlog_planner_backend", lambda **kwargs: object())

    exit_code = main([
        "plan-backlog",
        "--project",
        "demo",
        "--goal",
        "Ship autonomous roadmap governance.",
        "--mode",
        "strong-model",
        "--execute-planner",
    ])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert '"selected_epic_id": "epic-0001"' in captured.out
    assert "demo-20260512-add-backlog-planner" in captured.out


def test_run_backlog_wires_selected_epic_and_release_flags(monkeypatch, capsys, tmp_path) -> None:
    seen_kwargs: dict[str, object] = {}
    result = SimpleNamespace(
        selected_epic_id="run-backlog",
        plan_path=tmp_path / "runs" / "backlog_plan.json",
        objective_path=tmp_path / "objectives" / "run-backlog.yaml",
        release=SimpleNamespace(
            release_id="run-backlog-20260512",
            run_id="run-1",
            summary_path=tmp_path / "runs" / "summary.json",
            log_path=tmp_path / "runs" / "release.log",
            decision="accepted",
            task_results=[],
        ),
    )

    def fake_run_backlog(**kwargs):
        seen_kwargs.update(kwargs)
        return result

    monkeypatch.setattr(cli_module, "run_backlog", fake_run_backlog)
    monkeypatch.setattr(cli_module, "_codex_backlog_planner_backend", lambda **kwargs: object())

    exit_code = main(
        [
            "run-backlog",
            "--project",
            "demo",
            "--epic-id",
            "run-backlog",
            "--goal",
            "Move toward fully autonomous roadmap-driven development",
            "--execute-planner",
            "--merge-on-accept",
            "--continue-on-failure",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert seen_kwargs["selected_epic_id"] == "run-backlog"
    assert seen_kwargs["mode"] == "strong-model"
    assert seen_kwargs["merge_on_accept"] is True
    assert seen_kwargs["stop_on_failure"] is False
    assert '"selected_epic_id": "run-backlog"' in captured.out
    assert '"release_id": "run-backlog-20260512"' in captured.out


def test_run_backlog_requires_execute_planner(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "run-backlog",
                "--project",
                "demo",
                "--epic-id",
                "run-backlog",
                "--goal",
                "Move toward fully autonomous roadmap-driven development",
            ]
        )

    captured = capsys.readouterr()

    assert error.value.code == 2
    assert "run-backlog requires --execute-planner" in captured.err


def test_run_backlog_surfaces_planner_failure(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_module, "_codex_backlog_planner_backend", lambda **kwargs: object())

    def failing_run_backlog(**_kwargs):
        raise RuntimeError("backlog planner command failed (codex exec): planner crashed")

    monkeypatch.setattr(cli_module, "run_backlog", failing_run_backlog)

    with pytest.raises(SystemExit) as error:
        main(
            [
                "run-backlog",
                "--project",
                "demo",
                "--epic-id",
                "run-backlog",
                "--goal",
                "Move toward fully autonomous roadmap-driven development",
                "--execute-planner",
            ]
        )

    captured = capsys.readouterr()

    assert error.value.code == 2
    assert "backlog planner command failed" in captured.err


def test_run_backlog_surfaces_run_objective_failure(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_module, "_codex_backlog_planner_backend", lambda **kwargs: object())

    def failing_run_backlog(**_kwargs):
        raise RuntimeError("run-objective failed: release execution failed")

    monkeypatch.setattr(cli_module, "run_backlog", failing_run_backlog)

    with pytest.raises(SystemExit) as error:
        main(
            [
                "run-backlog",
                "--project",
                "demo",
                "--epic-id",
                "run-backlog",
                "--goal",
                "Move toward fully autonomous roadmap-driven development",
                "--execute-planner",
            ]
        )

    captured = capsys.readouterr()

    assert error.value.code == 2
    assert "run-objective failed" in captured.err


def test_cleanup_command_is_registered(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["cleanup", "--help"])

    captured = capsys.readouterr()

    assert error.value.code == 0
    assert "--release" in captured.out
    assert "--force" in captured.out
    assert "--include-integration-branch" in captured.out


def test_cleanup_command_outputs_report(monkeypatch, capsys, tmp_path) -> None:
    seen_kwargs: dict[str, object] = {}
    report = SimpleNamespace(
        project_id="demo",
        release_id="v1.0.0",
        dry_run=True,
        worktree_paths=[tmp_path / "worktrees" / "v1.0.0-task"],
        task_branches=["agent/v1.0.0/task"],
        integration_branch=None,
        removed_worktrees=[],
        deleted_branches=[],
        errors=[],
    )

    def fake_cleanup_release_artifacts(**kwargs):
        seen_kwargs.update(kwargs)
        return report

    monkeypatch.setattr(cli_module, "cleanup_release_artifacts", fake_cleanup_release_artifacts)

    exit_code = main(["cleanup", "--project", "demo", "--release", "v1.0.0"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert seen_kwargs["force"] is False
    assert '"dry_run": true' in captured.out
    assert '"agent/v1.0.0/task"' in captured.out


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


def test_run_objective_wires_planning_and_release_flags(monkeypatch, capsys, tmp_path) -> None:
    seen_kwargs: dict[str, object] = {}
    result = SimpleNamespace(
        release_id="v1.0.0",
        planning=SimpleNamespace(
            plan_path=tmp_path / "runs" / "plan" / "contract_plan.json",
            written_contract_paths=[tmp_path / "contracts" / "demo.yaml"],
        ),
        release=SimpleNamespace(
            release_id="v1.0.0",
            run_id="run-1",
            summary_path=tmp_path / "runs" / "summary.json",
            log_path=tmp_path / "runs" / "release.log",
            decision="accepted",
            task_results=[],
        ),
    )

    def fake_run_objective(**kwargs):
        seen_kwargs.update(kwargs)
        return result

    monkeypatch.setattr(cli_module, "run_objective", fake_run_objective)
    monkeypatch.setattr(cli_module, "_codex_planner_backend", lambda **kwargs: object())

    exit_code = main(
        [
            "run-objective",
            "--project",
            "demo",
            "--objective",
            str(tmp_path / "objective.yaml"),
            "--strong-model",
            "--execute-planner",
            "--merge-on-accept",
            "--continue-on-failure",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert seen_kwargs["planning_mode"] == "strong-model"
    assert seen_kwargs["merge_on_accept"] is True
    assert seen_kwargs["stop_on_failure"] is False
    assert '"release_id": "v1.0.0"' in captured.out


def test_execute_planner_requires_strong_model_mode(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["plan-release", "--objective", "objective.yaml", "--execute-planner"])

    captured = capsys.readouterr()

    assert error.value.code == 2
    assert "--execute-planner requires --mode strong-model" in captured.err


def test_run_objective_strong_model_requires_execute_planner(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["run-objective", "--project", "demo", "--objective", "objective.yaml", "--strong-model"])

    captured = capsys.readouterr()

    assert error.value.code == 2
    assert "run-objective --mode strong-model requires --execute-planner" in captured.err
