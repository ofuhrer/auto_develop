from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_devloop import cli as cli_module
from agentic_devloop.cli import main
from agentic_devloop.models import (
    BacklogEvidenceManifest,
    ContractPlan,
    GeneratedContract,
    GovernorContinuationAction,
    GovernorContinuationStopReason,
    GovernorCycleContinuation,
    GovernorStopReason,
    TaskContract,
)
from agentic_devloop.planning import ContractPlanResult
from agentic_devloop.cost_runtime_governance import build_cost_runtime_governance_decision
from agentic_devloop.governor import _build_execution_strategy_inputs
from agentic_devloop.models import BacklogEpic, BacklogPlan, ReleaseObjective
from agentic_devloop.supervisor_decisions import write_supervisor_decision_artifact


def test_cli_help_exits_successfully(capsys) -> None:
    exit_code = main([])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "agent-loop" in captured.out


def test_init_prints_project_and_repo(capsys) -> None:
    exit_code = main(["init", "--project", "demo", "--repo", "/tmp/demo"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "project=demo" in captured.out
    assert "repo=/tmp/demo" in captured.out


def test_config_prints_project_config(capsys) -> None:
    exit_code = main(["config", "--project", "auto_develop"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert '"project_id": "auto_develop"' in captured.out


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
    assert "--release-finalize" in captured.out
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
        feature_review_path=tmp_path / "runs" / "feature_review.json",
        feature_review_recheck_path=tmp_path / "runs" / "feature_review_recheck.json",
        final_review_continuation_decision_path=tmp_path / "runs" / "final_review_continuation_decision.json",
        final_integration_verification_path=tmp_path / "runs" / "final_integration_verification.json",
        feature_review_prompt_path=tmp_path / "runs" / "feature_review_prompt.md",
        feature_review_stdout_path=tmp_path / "runs" / "feature_review_stdout.log",
        feature_review_stderr_path=tmp_path / "runs" / "feature_review_stderr.log",
        feature_review_metadata_path=tmp_path / "runs" / "feature_review_metadata.json",
        feature_review_output_normalization_decision_path=tmp_path / "runs" / "feature_review_output_normalization_decision.json",
        feature_review_normalized_artifact_path=tmp_path / "runs" / "normalized_feature_review_decision.json",
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
    assert '"feature_review_prompt_path":' in captured.out
    assert '"final_review_continuation_decision_path":' in captured.out
    assert '"final_integration_verification_path":' in captured.out
    assert '"scope_risk_budget_policy_decision_paths":' in captured.out
    assert '"scope_risk_budget_policy_gate":' in captured.out


def test_run_release_command_outputs_finalization_gate(monkeypatch, capsys, tmp_path) -> None:
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
        decision="needs_revision",
        finalization_gate={
            "allowed": False,
            "reason": "unresolved_required_findings",
            "unresolved_required_finding_ids": ["finding-required-1"],
            "decision": "needs_revision",
        },
        task_results=[],
    )

    monkeypatch.setattr(cli_module, "run_release", lambda **kwargs: result)

    exit_code = main(["run-release", "--project", "demo", "--release", "v1.0.0"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert '"finalization_gate": {' in captured.out
    assert '"reason": "unresolved_required_findings"' in captured.out
    assert '"unresolved_required_finding_ids": [' in captured.out


def test_run_release_command_outputs_open_finalization_gate_after_review_flow(monkeypatch, capsys, tmp_path) -> None:
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
        finalization_gate={
            "allowed": True,
            "reason": "allowed",
            "unresolved_required_finding_ids": [],
            "decision": "accepted",
        },
        task_results=[],
    )

    monkeypatch.setattr(cli_module, "run_release", lambda **kwargs: result)

    exit_code = main(["run-release", "--project", "demo", "--release", "v1.0.0"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert '"finalization_gate": {' in captured.out
    assert '"allowed": true' in captured.out
    assert '"reason": "allowed"' in captured.out
    assert '"decision": "accepted"' in captured.out


def test_backlog_execution_strategy_inputs_consume_cost_runtime_governance(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    prior_release_run_dir = runs_dir / "20260514T010203Z_demo_release"
    prior_release_run_dir.mkdir(parents=True)
    metrics_path = prior_release_run_dir / "release_metrics.json"
    metrics_path.write_text(
        json.dumps(
            {
                "run_id": "demo-run",
                "release_id": "demo",
                "decision": "accepted",
                "totals": {
                    "prompt_chars": 1_100_000,
                    "context_chars": 900_000,
                },
                "compact_governance": {
                    "review_wave_count": 3,
                    "feature_review_repair_wave_count": 2,
                    "model_fallback_count": 3,
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    tuning_path = prior_release_run_dir / "release_tuning.md"
    tuning_path.write_text("# tuning\n", encoding="utf-8")
    decision = build_cost_runtime_governance_decision(
        decision_id="demo",
        release_id="demo",
        decided_by="test",
        budget_class="L",
        release_metrics_path=metrics_path,
        release_tuning_path=tuning_path,
    )
    write_supervisor_decision_artifact(release_bundle_path=prior_release_run_dir, decision=decision)

    plan = BacklogPlan(
        project_id="demo",
        goal="demo",
        roadmap_path=tmp_path / "roadmap.md",
        epics=[],
        selected_epic_id=None,
        state_review_snapshot_path=None,
        state_refresh_summary_path=None,
    )
    epic = BacklogEpic(
        epic_id="demo-epic",
        title="Demo",
        objective="Demo objective",
        rationale="Demo rationale",
        priority=1,
        acceptance_criteria=["ok"],
        suggested_release_id="demo",
    )
    objective = ReleaseObjective(
        release_id="demo",
        title="Demo",
        objective="Demo",
        non_goals=["none"],
        acceptance_criteria=["ok"],
    )

    selector_inputs = _build_execution_strategy_inputs(
        plan=plan,
        epic=epic,
        objective=objective,
        runs_dir=runs_dir,
    )

    assert selector_inputs["cohesive_scope"] is False
    assert selector_inputs["coupled_tasks"] is True
    assert selector_inputs["one_shot_recommendation_hint"] is True
    assert selector_inputs["cost_runtime_governance_decision_path"] is not None


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


def test_plan_release_outputs_strategy_artifact_paths_when_present(monkeypatch, capsys, tmp_path) -> None:
    plan_path = tmp_path / "runs" / "contract_plan.json"
    selection_path = tmp_path / "runs" / "execution_strategy_selection.json"
    decision_path = tmp_path / "runs" / "supervisor_decision.json"
    one_shot_input_path = tmp_path / "runs" / "one_shot_execution_input.json"
    result = ContractPlanResult(
        release_id="demo-20260513",
        plan_path=plan_path,
        plan=ContractPlan(release_id="demo-20260513"),
        written_contract_paths=[],
        execution_strategy_selection_path=selection_path,
        supervisor_decision_path=decision_path,
        one_shot_execution_input_path=one_shot_input_path,
    )

    monkeypatch.setattr(cli_module, "plan_release_contracts", lambda **_kwargs: result)

    exit_code = main(["plan-release", "--objective", "demo.yaml"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert '"execution_strategy_selection_path":' in captured.out
    assert str(selection_path) in captured.out
    assert '"supervisor_decision_path":' in captured.out
    assert str(decision_path) in captured.out
    assert '"one_shot_execution_input_path":' in captured.out
    assert str(one_shot_input_path) in captured.out


def test_run_objective_command_is_registered(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["run-objective", "--help"])

    captured = capsys.readouterr()

    assert error.value.code == 0
    assert "--objective" in captured.out
    assert "--execute-planner" in captured.out
    assert "--merge-on-accept" in captured.out


def test_run_objective_outputs_strategy_artifact_paths_when_present(monkeypatch, capsys, tmp_path) -> None:
    plan_path = tmp_path / "runs" / "contract_plan.json"
    selection_path = tmp_path / "runs" / "execution_strategy_selection.json"
    decision_path = tmp_path / "runs" / "supervisor_decision.json"
    one_shot_input_path = tmp_path / "runs" / "one_shot_execution_input.json"
    planning = ContractPlanResult(
        release_id="demo-20260513",
        plan_path=plan_path,
        plan=ContractPlan(release_id="demo-20260513"),
        written_contract_paths=[],
        execution_strategy_selection_path=selection_path,
        supervisor_decision_path=decision_path,
        one_shot_execution_input_path=one_shot_input_path,
    )
    result = SimpleNamespace(release_id="demo-20260513", planning=planning, release=None)

    monkeypatch.setattr(cli_module, "run_objective", lambda **_kwargs: result)

    exit_code = main(["run-objective", "--project", "demo", "--objective", "demo.yaml"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert '"execution_strategy_selection_path":' in captured.out
    assert str(selection_path) in captured.out
    assert '"supervisor_decision_path":' in captured.out
    assert str(decision_path) in captured.out
    assert '"one_shot_execution_input_path":' in captured.out
    assert str(one_shot_input_path) in captured.out
    assert '"release": null' in captured.out


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


def test_run_governor_command_is_registered(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["run-governor", "--help"])

    captured = capsys.readouterr()

    assert error.value.code == 0
    assert "--epic-count" in captured.out
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
        contract_plan_path=tmp_path / "runs" / "contract_plan.json",
        plan=SimpleNamespace(repo_state_updates=[], roadmap_updates=[]),
        release=SimpleNamespace(
            release_id="run-backlog-20260512",
            run_id="run-1",
            summary_path=tmp_path / "runs" / "summary.json",
            log_path=tmp_path / "runs" / "release.log",
            review_path=tmp_path / "runs" / "review.md",
            metrics_path=tmp_path / "runs" / "metrics.json",
            budget_path=tmp_path / "runs" / "budget.json",
            tuning_path=tmp_path / "runs" / "tuning.md",
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


def test_run_backlog_uses_planner_selected_epic_when_epic_id_is_omitted(monkeypatch, capsys, tmp_path) -> None:
    seen_kwargs: dict[str, object] = {}
    result = SimpleNamespace(
        selected_epic_id="planner-selected",
        plan_path=tmp_path / "runs" / "backlog_plan.json",
        objective_path=tmp_path / "objectives" / "planner-selected.yaml",
        contract_plan_path=tmp_path / "runs" / "contract_plan.json",
        plan=SimpleNamespace(repo_state_updates=[], roadmap_updates=[]),
        release=SimpleNamespace(
            release_id="planner-selected-20260512",
            run_id="run-1",
            summary_path=tmp_path / "runs" / "summary.json",
            log_path=tmp_path / "runs" / "release.log",
            review_path=tmp_path / "runs" / "review.md",
            metrics_path=tmp_path / "runs" / "metrics.json",
            budget_path=tmp_path / "runs" / "budget.json",
            tuning_path=tmp_path / "runs" / "tuning.md",
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
            "--goal",
            "Move toward fully autonomous roadmap-driven development",
            "--execute-planner",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert seen_kwargs["selected_epic_id"] is None
    assert '"selected_epic_id": "planner-selected"' in captured.out


def test_run_backlog_writes_governor_lifecycle_events_with_artifacts(monkeypatch, capsys, tmp_path) -> None:
    run_id = "20260513T000000Z_demo_governor"
    release_result = SimpleNamespace(
        release_id="demo-20260513",
        run_id="run-1",
        summary_path=tmp_path / "runs" / "summary.json",
        log_path=tmp_path / "runs" / "release.log",
        review_path=tmp_path / "runs" / "review.md",
        metrics_path=tmp_path / "runs" / "metrics.json",
        budget_path=tmp_path / "runs" / "budget.json",
        tuning_path=tmp_path / "runs" / "tuning.md",
        decision="accepted",
        task_results=[],
    )
    result = SimpleNamespace(
        selected_epic_id="epic-1",
        plan_path=tmp_path / "runs" / "backlog_plan.json",
        objective_path=tmp_path / "objectives" / "demo-20260513.yaml",
        contract_plan_path=tmp_path / "runs" / "contract_plan.json",
        plan=SimpleNamespace(repo_state_updates=["refresh backlog_state notes"], roadmap_updates=[]),
        release=release_result,
    )

    def fake_run_backlog(**kwargs):
        kwargs["progress"]("event=release_started run_id=run-1 release=demo-20260513 tasks=1 mode=sequential")
        kwargs["progress"]("event=repair_decision task=demo-1 attempt=1 decision=retry action=release_resume")
        kwargs["progress"]("event=task_resumed task=demo-1 attempt=1")
        kwargs["progress"]("event=repair_decision task=demo-1 attempt=2 decision=retry action=planner_contract_normalization")
        kwargs["progress"]("event=release_merged target=main")
        return result

    monkeypatch.setattr(cli_module, "_make_governor_run_id", lambda **_kwargs: run_id)
    monkeypatch.setattr(cli_module, "run_backlog", fake_run_backlog)
    monkeypatch.setattr(cli_module, "_codex_backlog_planner_backend", lambda **kwargs: object())

    exit_code = main(
        [
            "run-backlog",
            "--project",
            "demo",
            "--goal",
            "Run one epic with lifecycle logging",
            "--execute-planner",
            "--runs-dir",
            str(tmp_path / "runs"),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert '"selected_epic_id": "epic-1"' in captured.out
    events_path = tmp_path / "runs" / run_id / "events.jsonl"
    assert events_path.exists()
    events_text = events_path.read_text(encoding="utf-8")
    assert '"event_type": "backlog_planning_completed"' in events_text
    assert '"event_type": "objective_ready"' in events_text
    assert '"event_type": "contract_plan_completed"' in events_text
    assert '"event_type": "contract_normalization"' in events_text
    assert '"event_type": "repair_decision"' in events_text
    assert '"event_type": "release_completed"' in events_text
    assert '"event_type": "finalization_completed"' in events_text
    assert '"event_type": "state_refreshed"' in events_text
    assert str(result.plan_path) in events_text
    assert str(result.contract_plan_path) in events_text
    assert str(release_result.summary_path) in events_text


def test_run_governor_wires_epic_count_and_writes_parent_events(monkeypatch, capsys, tmp_path) -> None:
    run_id = "20260513T000000Z_demo_governor"
    seen_kwargs: dict[str, object] = {}
    for name in (
        "summary.json",
        "release.log",
        "review.md",
        "metrics.json",
        "budget.json",
        "tuning.md",
        "backlog_plan.json",
        "contract_plan.json",
        "execution_strategy_selection.json",
        "supervisor_decision.json",
        "release_soft_gate_decision.json",
        "state_review_snapshot.json",
        "state_refresh_summary.json",
        "feature_review.json",
        "feature_review_recheck.json",
        "final_integration_verification.json",
    ):
        path = tmp_path / "runs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    objective_path = tmp_path / "objectives" / "demo-epic-1.yaml"
    objective_path.parent.mkdir(parents=True, exist_ok=True)
    objective_path.write_text("release_id: demo-epic-1\n", encoding="utf-8")
    release_result = SimpleNamespace(
        release_id="demo-epic-1",
        run_id="run-1",
        summary_path=tmp_path / "runs" / "summary.json",
        log_path=tmp_path / "runs" / "release.log",
        review_path=tmp_path / "runs" / "review.md",
        metrics_path=tmp_path / "runs" / "metrics.json",
        budget_path=tmp_path / "runs" / "budget.json",
        tuning_path=tmp_path / "runs" / "tuning.md",
        final_integration_verification_path=tmp_path / "runs" / "final_integration_verification.json",
        decision="accepted",
        task_results=[],
    )
    cycle = SimpleNamespace(
        selected_epic_id="epic-1",
        plan_path=tmp_path / "runs" / "backlog_plan.json",
        objective_path=objective_path,
        contract_plan_path=tmp_path / "runs" / "contract_plan.json",
        release=release_result,
        evidence_manifest=BacklogEvidenceManifest(
            backlog_plan_path=tmp_path / "runs" / "backlog_plan.json",
            generated_objective_path=tmp_path / "objectives" / "demo-epic-1.yaml",
            contract_plan_path=tmp_path / "runs" / "contract_plan.json",
            execution_strategy_selection_path=tmp_path / "runs" / "execution_strategy_selection.json",
            supervisor_decision_path=tmp_path / "runs" / "supervisor_decision.json",
            release_summary_path=tmp_path / "runs" / "summary.json",
            release_log_path=tmp_path / "runs" / "release.log",
            release_review_path=tmp_path / "runs" / "review.md",
            release_metrics_path=tmp_path / "runs" / "metrics.json",
            release_budget_path=tmp_path / "runs" / "budget.json",
            release_tuning_path=tmp_path / "runs" / "tuning.md",
            release_soft_gate_decision_path=tmp_path / "runs" / "release_soft_gate_decision.json",
            feature_review_path=tmp_path / "runs" / "feature_review.json",
            feature_review_recheck_path=tmp_path / "runs" / "feature_review_recheck.json",
            finalization_summary_path=tmp_path / "runs" / "summary.json",
            repo_state_proposal_plan_path=tmp_path / "runs" / "backlog_plan.json",
            state_review_snapshot_path=tmp_path / "runs" / "state_review_snapshot.json",
            state_refresh_summary_path=tmp_path / "runs" / "state_refresh_summary.json",
        ),
    )
    result = SimpleNamespace(
        project_id="demo",
        requested_epic_count=2,
        attempted_epic_count=1,
        accepted_epic_count=1,
        stop_reason=GovernorStopReason.RELEASE_NOT_ACCEPTED,
        cycles=[cycle],
    )

    def fake_run_governor(**kwargs):
        seen_kwargs.update(kwargs)
        return result

    cleanup_report = SimpleNamespace(
        project_id="demo",
        release_id="demo-epic-1",
        dry_run=True,
        worktree_paths=[],
        task_branches=[],
        integration_branch=None,
        removed_worktrees=[],
        deleted_branches=[],
        errors=[],
    )

    monkeypatch.setattr(cli_module, "_make_governor_run_id", lambda **_kwargs: run_id)
    monkeypatch.setattr(cli_module, "run_governor", fake_run_governor)
    monkeypatch.setattr(cli_module, "_codex_backlog_planner_backend", lambda **kwargs: object())
    monkeypatch.setattr(cli_module, "cleanup_release_artifacts", lambda **_kwargs: cleanup_report)

    exit_code = main(
        [
            "run-governor",
            "--project",
            "demo",
            "--goal",
            "Run repeated epics",
            "--epic-count",
            "2",
            "--execute-planner",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--release-finalize",
            "push-feature",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert seen_kwargs["epic_count"] == 2
    assert seen_kwargs["release_finalize"] == "push-feature"
    assert '"requested_epic_count": 2' in captured.out
    assert '"attempted_epic_count": 1' in captured.out
    assert '"accepted_epic_count": 1' in captured.out
    assert '"stop_reason": "release_not_accepted"' in captured.out
    assert '"stop_context": {' in captured.out
    assert '"category": "non_accepted_release"' in captured.out
    assert '"cycle_index": 1' in captured.out
    assert '"epic_id": "epic-1"' in captured.out
    assert '"release_id": "demo-epic-1"' in captured.out
    assert '"cleanup_result": {' in captured.out
    assert '"evidence_manifest": {' in captured.out
    assert '"dry_run": true' in captured.out
    assert '"governor_log_path":' in captured.out
    assert '"governor_events_path":' in captured.out
    assert str(tmp_path / "runs" / run_id / "governor.log") in captured.out
    assert str(tmp_path / "runs" / run_id / "events.jsonl") in captured.out
    events_text = (tmp_path / "runs" / run_id / "events.jsonl").read_text(encoding="utf-8")
    assert '"event_type": "governor_started"' in events_text
    assert '"event_type": "state_review_completed"' in events_text
    assert '"event_type": "state_refresh_summary"' in events_text
    assert '"event_type": "backlog_planning_completed"' in events_text
    assert '"event_type": "backlog_selection_completed"' in events_text
    assert '"event_type": "epic_selected"' in events_text
    assert '"event_type": "objective_generation_completed"' in events_text
    assert '"event_type": "contract_generation_completed"' in events_text
    assert '"event_type": "child_release_started"' in events_text
    assert '"event_type": "child_release_completed"' in events_text
    assert '"event_type": "release_completed"' in events_text
    assert '"event_type": "feature_review_completed"' in events_text
    assert '"event_type": "final_verification_completed"' in events_text
    assert '"event_type": "repair_decision"' in events_text
    assert '"event_type": "finalization_decision"' in events_text
    assert '"event_type": "finalization_completed"' in events_text
    assert '"event_type": "cleanup_eligibility_evaluated"' in events_text
    assert '"event_type": "stop_reason_recorded"' in events_text
    assert '"stop_category": "non_accepted_release"' in events_text
    assert "cleanup_handoff" in events_text
    assert (tmp_path / "runs" / run_id / "governor.log").exists()
    assert (tmp_path / "runs" / run_id / "governor.raw.log").exists()
    assert (tmp_path / "runs" / run_id / "events.jsonl").exists()
    cleanup_path = tmp_path / "runs" / run_id / "cleanup" / "cycle_001_demo-epic-1_cleanup.json"
    assert cleanup_path.exists()
    assert str(cleanup_path) in events_text
    assert str(cycle.evidence_manifest.contract_plan_path) in events_text
    assert str(cycle.evidence_manifest.generated_objective_path) in events_text
    assert str(cycle.evidence_manifest.release_summary_path) in events_text
    assert str(cycle.evidence_manifest.release_log_path) in events_text
    assert str(cycle.evidence_manifest.release_review_path) in events_text
    assert str(cycle.evidence_manifest.supervisor_decision_path) in events_text
    assert str(cycle.evidence_manifest.release_soft_gate_decision_path) in events_text
    assert str(cycle.evidence_manifest.feature_review_path) in events_text
    assert str(cycle.evidence_manifest.feature_review_recheck_path) in events_text
    assert str(release_result.final_integration_verification_path) in events_text
    assert str(cycle.evidence_manifest.state_review_snapshot_path) in events_text
    assert str(cycle.evidence_manifest.state_refresh_summary_path) in events_text
    assert str(cycle.evidence_manifest.finalization_summary_path) in events_text
    assert str(cycle.evidence_manifest.repo_state_proposal_plan_path) in events_text
    assert '"event_type": "governor_completed"' in events_text
    records = [
        json.loads(line)
        for line in (tmp_path / "runs" / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    child_start = next(record for record in records if record["event_type"] == "child_release_started")
    assert child_start["context"]["phase"] == "child_release_started"
    assert child_start["context"]["cycle_index"] == 1
    assert child_start["context"]["epic_id"] == "epic-1"
    assert child_start["context"]["release_id"] == "demo-epic-1"
    finalization_decision = next(
        record for record in records if record["event_type"] == "finalization_decision"
    )
    assert finalization_decision["context"]["phase"] == "finalization_decision"
    assert finalization_decision["context"]["decision"] == "accepted"
    assert finalization_decision["context"]["cycle_index"] == 1
    assert finalization_decision["context"]["epic_id"] == "epic-1"
    assert finalization_decision["context"]["release_id"] == "demo-epic-1"
    finalization_completed = next(
        record for record in records if record["event_type"] == "finalization_completed"
    )
    assert finalization_completed["context"]["phase"] == "finalization_completed"
    assert finalization_completed["context"]["decision"] == "accepted"
    assert finalization_completed["context"]["outcome"] == "completed"
    assert finalization_completed["context"]["details"]["mode"] == "push-feature"


def test_run_governor_next_epic_event_uses_upcoming_cycle_context(monkeypatch, tmp_path) -> None:
    run_id = "20260513T000000Z_demo_governor"
    for name in ("cycle1_plan.json", "cycle2_plan.json", "summary.json", "release.log", "review.md"):
        path = tmp_path / "runs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    objective_path = tmp_path / "objectives" / "demo-epic-1.yaml"
    objective_path.parent.mkdir(parents=True, exist_ok=True)
    objective_path.write_text("release_id: demo-epic-1\n", encoding="utf-8")
    release_result = SimpleNamespace(
        release_id="demo-epic-1",
        run_id="run-1",
        summary_path=tmp_path / "runs" / "summary.json",
        log_path=tmp_path / "runs" / "release.log",
        review_path=tmp_path / "runs" / "review.md",
        decision="accepted",
        task_results=[],
    )
    first_cycle = SimpleNamespace(
        selected_epic_id="epic-1",
        plan_path=tmp_path / "runs" / "cycle1_plan.json",
        objective_path=objective_path,
        contract_plan_path=None,
        release=release_result,
        evidence_manifest=BacklogEvidenceManifest(backlog_plan_path=tmp_path / "runs" / "cycle1_plan.json"),
    )
    second_cycle = SimpleNamespace(
        selected_epic_id="next-epic",
        release_id="next-release",
        plan_path=tmp_path / "runs" / "cycle2_plan.json",
        objective_path=None,
        contract_plan_path=None,
        release=None,
        evidence_manifest=BacklogEvidenceManifest(backlog_plan_path=tmp_path / "runs" / "cycle2_plan.json"),
    )
    result = SimpleNamespace(
        project_id="demo",
        requested_epic_count=2,
        attempted_epic_count=2,
        accepted_epic_count=1,
        stop_reason=GovernorStopReason.REQUESTED_EPIC_COUNT_REACHED,
        cycles=[first_cycle, second_cycle],
    )

    monkeypatch.setattr(cli_module, "_make_governor_run_id", lambda **_kwargs: run_id)
    monkeypatch.setattr(cli_module, "run_governor", lambda **_kwargs: result)
    monkeypatch.setattr(cli_module, "_codex_backlog_planner_backend", lambda **kwargs: object())

    assert (
        main(
            [
                "run-governor",
                "--project",
                "demo",
                "--goal",
                "Run repeated epics",
                "--epic-count",
                "2",
                "--execute-planner",
                "--runs-dir",
                str(tmp_path / "runs"),
            ]
        )
        == 0
    )

    records = [
        json.loads(line)
        for line in (tmp_path / "runs" / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    release_completed = next(record for record in records if record["event_type"] == "release_completed")
    next_epic = next(record for record in records if record["event_type"] == "next_epic_selected")
    assert records.index(release_completed) < records.index(next_epic)
    assert next_epic["context"]["phase"] == "next_epic_selected"
    assert next_epic["context"]["cycle_index"] == 2
    assert next_epic["context"]["epic_id"] == "next-epic"
    assert next_epic["context"]["release_id"] == "next-release"


def test_run_governor_outputs_blocked_finalization_state(monkeypatch, capsys, tmp_path) -> None:
    run_id = "20260513T000000Z_demo_governor"
    release_result = SimpleNamespace(
        release_id="demo-epic-1",
        run_id="run-1",
        summary_path=tmp_path / "runs" / "summary.json",
        log_path=tmp_path / "runs" / "release.log",
        review_path=tmp_path / "runs" / "review.md",
        metrics_path=tmp_path / "runs" / "metrics.json",
        budget_path=tmp_path / "runs" / "budget.json",
        tuning_path=tmp_path / "runs" / "tuning.md",
        decision="accepted",
        task_results=[],
    )
    cycle = SimpleNamespace(
        selected_epic_id="epic-1",
        plan_path=tmp_path / "runs" / "backlog_plan.json",
        objective_path=tmp_path / "objectives" / "demo-epic-1.yaml",
        contract_plan_path=tmp_path / "runs" / "contract_plan.json",
        release=release_result,
        finalization_policy="push-feature",
        finalization_result={
            "gate": {
                "allowed": False,
                "reason": "unresolved_required_findings",
                "unresolved_required_finding_ids": ["finding-required-1"],
                "decision": "accepted",
            }
        },
        blocked_finalization={
            "type": "finalization_gate_blocked",
            "policy": "push-feature",
            "reason": "unresolved_required_findings",
            "decision": "accepted",
            "unresolved_required_finding_ids": ["finding-required-1"],
        },
    )
    result = SimpleNamespace(
        project_id="demo",
        requested_epic_count=2,
        attempted_epic_count=1,
        accepted_epic_count=1,
        stop_reason=GovernorStopReason.BLOCKED_FINALIZATION,
        cycles=[cycle],
    )

    monkeypatch.setattr(cli_module, "_make_governor_run_id", lambda **_kwargs: run_id)
    monkeypatch.setattr(cli_module, "run_governor", lambda **kwargs: result)
    monkeypatch.setattr(cli_module, "_codex_backlog_planner_backend", lambda **kwargs: object())
    monkeypatch.setattr(
        cli_module,
        "cleanup_release_artifacts",
        lambda **_kwargs: SimpleNamespace(
            project_id="demo",
            release_id="demo-epic-1",
            dry_run=True,
            worktree_paths=[],
            task_branches=[],
            integration_branch=None,
            removed_worktrees=[],
            deleted_branches=[],
            errors=[],
        ),
    )

    exit_code = main(
        [
            "run-governor",
            "--project",
            "demo",
            "--goal",
            "Run repeated epics",
            "--epic-count",
            "2",
            "--execute-planner",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--release-finalize",
            "push-feature",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert '"stop_reason": "blocked_finalization"' in captured.out
    assert '"stop_context": {' in captured.out
    assert '"category": "blocked_finalization"' in captured.out
    assert '"blocked_finalization": {' in captured.out
    assert '"type": "finalization_gate_blocked"' in captured.out
    assert '"finalization_policy": "push-feature"' in captured.out


def test_run_governor_outputs_pr_preparation_handoff_state(monkeypatch, capsys, tmp_path) -> None:
    run_id = "20260513T000000Z_demo_governor"
    release_result = SimpleNamespace(
        release_id="demo-epic-1",
        run_id="run-1",
        summary_path=tmp_path / "runs" / "summary.json",
        log_path=tmp_path / "runs" / "release.log",
        review_path=tmp_path / "runs" / "review.md",
        metrics_path=tmp_path / "runs" / "metrics.json",
        budget_path=tmp_path / "runs" / "budget.json",
        tuning_path=tmp_path / "runs" / "tuning.md",
        decision="accepted",
        task_results=[],
    )
    handoff_path = tmp_path / "runs" / "pr_handoff.json"
    cycle = SimpleNamespace(
        selected_epic_id="epic-1",
        plan_path=tmp_path / "runs" / "backlog_plan.json",
        objective_path=tmp_path / "objectives" / "demo-epic-1.yaml",
        contract_plan_path=tmp_path / "runs" / "contract_plan.json",
        release=release_result,
        finalization_policy="pr_preparation",
        finalization_result={
            "handoff_path": str(handoff_path),
            "policy": "pr_preparation",
        },
    )
    result = SimpleNamespace(
        project_id="demo",
        requested_epic_count=1,
        attempted_epic_count=1,
        accepted_epic_count=1,
        stop_reason=GovernorStopReason.REQUESTED_EPIC_COUNT_REACHED,
        cycles=[cycle],
    )

    monkeypatch.setattr(cli_module, "_make_governor_run_id", lambda **_kwargs: run_id)
    monkeypatch.setattr(cli_module, "run_governor", lambda **kwargs: result)
    monkeypatch.setattr(cli_module, "_codex_backlog_planner_backend", lambda **kwargs: object())
    monkeypatch.setattr(
        cli_module,
        "cleanup_release_artifacts",
        lambda **_kwargs: SimpleNamespace(
            project_id="demo",
            release_id="demo-epic-1",
            dry_run=True,
            worktree_paths=[],
            task_branches=[],
            integration_branch=None,
            removed_worktrees=[],
            deleted_branches=[],
            errors=[],
        ),
    )

    exit_code = main(
        [
            "run-governor",
            "--project",
            "demo",
            "--goal",
            "Run repeated epics",
            "--epic-count",
            "1",
            "--execute-planner",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--release-finalize",
            "push-feature",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert '"finalization_policy": "pr_preparation"' in captured.out
    assert '"finalization_result": {' in captured.out
    assert '"handoff_path":' in captured.out
    assert str(handoff_path) in captured.out


@pytest.mark.parametrize(
    ("stop_reason", "cycle", "expected_category"),
    [
        (
            GovernorStopReason.NO_ACTIONABLE_WORK,
            SimpleNamespace(
                selected_epic_id="epic-noop",
                release_id="no_actionable_work",
                release=None,
                plan_path=Path("/tmp/missing-plan.json"),
                objective_path=Path("/tmp/missing-objective.yaml"),
                contract_plan_path=None,
                evidence_manifest=None,
            ),
            "no_actionable_work",
        ),
        (
            GovernorStopReason.PLANNING_ONLY_STRATEGY,
            SimpleNamespace(
                selected_epic_id="epic-plan",
                release_id="demo-plan-only",
                release=None,
                plan_path=Path("/tmp/missing-plan.json"),
                objective_path=Path("/tmp/missing-objective.yaml"),
                contract_plan_path=None,
                evidence_manifest=None,
            ),
            "planning_only_strategy",
        ),
        (
            GovernorStopReason.STATE_REFRESH_FAILED,
            SimpleNamespace(
                selected_epic_id="epic-refresh",
                release_id="demo-refresh",
                release=None,
                plan_path=Path("/tmp/missing-plan.json"),
                objective_path=Path("/tmp/missing-objective.yaml"),
                contract_plan_path=None,
                governor_cycle_continuation=GovernorCycleContinuation(
                    action=GovernorContinuationAction.STOP,
                    stop_reason=GovernorContinuationStopReason.STATE_REFRESH_FAILED,
                ),
                evidence_manifest=BacklogEvidenceManifest(
                    state_refresh_error_path=Path("/tmp/missing-refresh-error.json"),
                ),
            ),
            "state_refresh_failure",
        ),
    ],
)
def test_run_governor_outputs_typed_stop_context_categories(
    monkeypatch, capsys, tmp_path, stop_reason, cycle, expected_category
) -> None:
    run_id = "20260513T000000Z_demo_governor"
    result = SimpleNamespace(
        project_id="demo",
        requested_epic_count=2,
        attempted_epic_count=1,
        accepted_epic_count=0,
        stop_reason=stop_reason,
        cycles=[cycle],
    )
    monkeypatch.setattr(cli_module, "_make_governor_run_id", lambda **_kwargs: run_id)
    monkeypatch.setattr(cli_module, "run_governor", lambda **kwargs: result)
    monkeypatch.setattr(cli_module, "_codex_backlog_planner_backend", lambda **kwargs: object())

    exit_code = main(
        [
            "run-governor",
            "--project",
            "demo",
            "--goal",
            "Run repeated epics",
            "--epic-count",
            "2",
            "--execute-planner",
            "--runs-dir",
            str(tmp_path / "runs"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert '"stop_context": {' in captured.out
    assert f'"category": "{expected_category}"' in captured.out
    events_text = (tmp_path / "runs" / run_id / "events.jsonl").read_text(encoding="utf-8")
    assert '"event_type": "stop_reason_recorded"' in events_text
    assert f'"stop_category": "{expected_category}"' in events_text


def test_run_governor_records_missing_credentials_stop_context_before_nonzero_exit(
    monkeypatch, capsys, tmp_path
) -> None:
    run_id = "20260513T000000Z_demo_governor"
    monkeypatch.setattr(cli_module, "_make_governor_run_id", lambda **_kwargs: run_id)
    monkeypatch.setattr(cli_module, "_codex_backlog_planner_backend", lambda **kwargs: object())

    def failing_run_governor(**_kwargs):
        raise RuntimeError("missing planner credentials: OPENAI_API_KEY")

    monkeypatch.setattr(cli_module, "run_governor", failing_run_governor)

    with pytest.raises(SystemExit) as error:
        main(
            [
                "run-governor",
                "--project",
                "demo",
                "--goal",
                "Run repeated epics",
                "--epic-count",
                "1",
                "--execute-planner",
                "--runs-dir",
                str(tmp_path / "runs"),
            ]
        )
    captured = capsys.readouterr()
    assert error.value.code == 2
    assert "missing planner credentials" in captured.err
    events_text = (tmp_path / "runs" / run_id / "events.jsonl").read_text(encoding="utf-8")
    assert '"event_type": "stop_reason_recorded"' in events_text
    assert '"stop_category": "missing_planner_credentials"' in events_text
    assert '"exception_class": "RuntimeError"' in events_text


def test_run_governor_records_hard_policy_stop_context_before_nonzero_exit(
    monkeypatch, capsys, tmp_path
) -> None:
    run_id = "20260513T000000Z_demo_governor"
    monkeypatch.setattr(cli_module, "_make_governor_run_id", lambda **_kwargs: run_id)
    monkeypatch.setattr(cli_module, "_codex_backlog_planner_backend", lambda **kwargs: object())

    def failing_run_governor(**_kwargs):
        raise RuntimeError("blocked by hard gate: forbidden path update")

    monkeypatch.setattr(cli_module, "run_governor", failing_run_governor)

    with pytest.raises(SystemExit) as error:
        main(
            [
                "run-governor",
                "--project",
                "demo",
                "--goal",
                "Run repeated epics",
                "--epic-count",
                "1",
                "--execute-planner",
                "--runs-dir",
                str(tmp_path / "runs"),
            ]
        )
    captured = capsys.readouterr()
    assert error.value.code == 2
    assert "blocked by hard gate" in captured.err
    events_text = (tmp_path / "runs" / run_id / "events.jsonl").read_text(encoding="utf-8")
    assert '"event_type": "stop_reason_recorded"' in events_text
    assert '"stop_category": "hard_policy_stop"' in events_text


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
