from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentic_devloop.budget import build_budget_ledger, build_tuning_report
from agentic_devloop.models import (
    Budget,
    Decision,
    EvidenceBundle,
    FailureDiagnosis,
    FailureDiagnosisGuidance,
    FailureDiagnosisInput,
    FailureDiagnosisSourceMetadata,
    FailureEvidenceExcerpt,
    ProjectConfig,
    ReleaseObjective,
    ReviewDecision,
    Reviewer,
    TaskContract,
    TaskRun,
    TaskState,
)
from agentic_devloop.yaml_io import load_yaml_model


ROOT = Path(__file__).resolve().parents[1]


def test_sample_yaml_files_validate() -> None:
    project = load_yaml_model(ROOT / "configs" / "rust_rockfall.yaml", ProjectConfig)
    objective = load_yaml_model(ROOT / "objectives" / "v0.8.0.yaml", ReleaseObjective)
    contract = load_yaml_model(ROOT / "contracts" / "rr-0001.yaml", TaskContract)

    assert project.project_id == "rust_rockfall"
    assert objective.release_id == "v0.8.0"
    assert contract.task_id == "rr-0001"


def test_missing_required_field_fails_clearly() -> None:
    with pytest.raises(ValidationError) as error:
        ProjectConfig.model_validate(
            {
                "project_id": "rust_rockfall",
                "repo_path": "/tmp/rust_rockfall",
                "default_base_branch": "main",
                "worktree_root": "/tmp/worktrees",
                "verification_profiles": {"default": {"commands": ["cargo test"]}},
                "budget": {
                    "max_executor_attempts_per_task": 2,
                    "max_strong_model_calls_per_release": 10,
                    "max_changed_files_per_task": 8,
                    "max_diff_lines_per_task": 600,
                },
            }
        )

    errors = error.value.errors()

    assert errors[0]["loc"] == ("executor",)
    assert errors[0]["type"] == "missing"


def test_runtime_state_models_validate() -> None:
    now = datetime.now(timezone.utc)

    run = TaskRun(
        task_id="rr-0001",
        state=TaskState.VERIFYING,
        worktree_path=Path("/tmp/worktrees/rr-0001"),
        branch="agent/v0.8.0/rr-0001",
        executor_attempts=1,
        started_at=now,
        updated_at=now,
        changed_files=[],
        diff_lines=0,
        verification_results=[],
    )
    evidence = EvidenceBundle(
        task_id="rr-0001",
        run_id="2026-05-12_v0.8.0",
        bundle_path=Path("runs/2026-05-12_v0.8.0/rr-0001"),
        contract_path=Path("runs/2026-05-12_v0.8.0/rr-0001/contract.yaml"),
        run_state_path=Path("runs/2026-05-12_v0.8.0/rr-0001/run_state.json"),
        executor_prompt_path=Path("runs/2026-05-12_v0.8.0/rr-0001/executor_prompt.md"),
        executor_stdout_path=Path("runs/2026-05-12_v0.8.0/rr-0001/executor_stdout.log"),
        executor_stderr_path=Path("runs/2026-05-12_v0.8.0/rr-0001/executor_stderr.log"),
        git_diff_path=Path("runs/2026-05-12_v0.8.0/rr-0001/git_diff.patch"),
        changed_files_path=Path("runs/2026-05-12_v0.8.0/rr-0001/changed_files.txt"),
        verification_log_path=Path("runs/2026-05-12_v0.8.0/rr-0001/verification.log"),
    )
    decision = ReviewDecision(
        task_id="rr-0001",
        decision=Decision.ACCEPTED,
        reviewer=Reviewer.DETERMINISTIC,
        rationale="Diff is within contract and verification passed.",
    )

    assert run.state == TaskState.VERIFYING
    assert evidence.task_id == "rr-0001"
    assert decision.decision == Decision.ACCEPTED


def test_failure_diagnosis_model_validates_and_serializes() -> None:
    diagnosis = FailureDiagnosis(
        diagnosis_inputs=[
            FailureDiagnosisInput(name="executor_exit_code", value="1", source="executor_result"),
            FailureDiagnosisInput(name="timed_out", value="false"),
        ],
        category="executor_error",
        confidence=0.86,
        supporting_evidence_excerpts=[
            FailureEvidenceExcerpt(
                source="executor_stderr.log",
                excerpt="traceback: missing dependency",
                path=Path("runs/example/task/evidence/executor_stderr.log"),
            )
        ],
        recommendation="Inspect the missing dependency and retry after fixing the environment.",
        guidance=FailureDiagnosisGuidance(
            retryable=True,
            escalate=False,
            retry_reason="The failure is consistent with an environment issue.",
        ),
        source_metadata=FailureDiagnosisSourceMetadata(
            backend="codex_cli",
            model="gpt-5.3-codex-spark",
            command=["codex", "run"],
            exit_code=1,
            timed_out=False,
            stdout_path=Path("runs/example/task/evidence/executor_stdout.log"),
            stderr_path=Path("runs/example/task/evidence/executor_stderr.log"),
        ),
    )

    dumped = diagnosis.model_dump(mode="json")

    assert dumped["category"] == "executor_error"
    assert dumped["guidance"]["retryable"] is True
    assert dumped["source_metadata"]["backend"] == "codex_cli"
    assert dumped["supporting_evidence_excerpts"][0]["source"] == "executor_stderr.log"


def test_failure_diagnosis_model_rejects_invalid_confidence() -> None:
    with pytest.raises(ValidationError, match="confidence"):
        FailureDiagnosis.model_validate(
            {
                "diagnosis_inputs": [],
                "category": "executor_error",
                "confidence": 1.5,
                "supporting_evidence_excerpts": [],
                "recommendation": "Retry later.",
                "guidance": {"retryable": True, "escalate": False},
                "source_metadata": {"backend": "codex_cli"},
            }
        )


def test_budget_ledger_captures_usage_and_signals() -> None:
    ledger = build_budget_ledger(release_metrics=_budget_metrics(), budget=_budget())
    usage = {entry.name: entry for entry in ledger.usage}

    assert ledger.release_id == "v1.2.3"
    assert usage["changed_files_per_task"].actual == 7
    assert usage["context_chars_per_task"].utilization == 0.933
    assert ledger.model_attempts[0].model == "gpt-5.3-codex-spark"
    assert any(outlier.task_id == "demo-0001" and outlier.metric == "changed_files" for outlier in ledger.task_size_outliers)
    assert any(signal.kind == "waste_signal" for signal in ledger.waste_signals)


def test_budget_tuning_report_renders_guidance() -> None:
    report = build_tuning_report(ledger=build_budget_ledger(release_metrics=_budget_metrics(False), budget=_budget()))
    rendered = report.render_markdown()

    assert "Budget tuning guidance for v1.2.3" in rendered
    assert "fallback model gpt-5.4-mini" in rendered
    assert "Split or narrow task demo-0001" in rendered


def _budget() -> Budget:
    return Budget(
        max_executor_attempts_per_task=2,
        max_strong_model_calls_per_release=5,
        max_changed_files_per_task=8,
        max_diff_lines_per_task=600,
        max_context_chars_per_task=30_000,
    )


def _budget_metrics(include_second_task: bool = True) -> dict[str, object]:
    tasks = [
        {
            "task_id": "demo-0001",
            "decision": "accepted",
            "bundle_path": "runs/2026-05-12_v1.2.3/demo-0001",
            "context_chars": 28000,
            "prompt_chars": 4200,
            "stdout_chars": 100,
            "stderr_chars": 40,
            "diff_lines": 540,
            "changed_file_count": 7,
            "verification_command_count": 4,
            "verification_duration_seconds": 12.0,
            "executor_attempts": [
                {"attempt": 1, "model": "gpt-5.3-codex-spark", "exit_code": 1, "duration_seconds": 2.0, "prompt_chars": 2400, "stdout_chars": 25, "stderr_chars": 10},
                {"attempt": 2, "model": "gpt-5.4-mini", "exit_code": 0, "duration_seconds": 1.1, "prompt_chars": 2600, "stdout_chars": 95, "stderr_chars": 50},
            ],
        }
    ]
    if include_second_task:
        tasks.append(
            {
                "task_id": "demo-0002",
                "decision": "accepted",
                "bundle_path": "runs/2026-05-12_v1.2.3/demo-0002",
                "context_chars": 5000,
                "prompt_chars": 800,
                "stdout_chars": 20,
                "stderr_chars": 10,
                "diff_lines": 20,
                "changed_file_count": 1,
                "verification_command_count": 1,
                "verification_duration_seconds": 1.0,
                "executor_attempts": [{"attempt": 1, "model": "gpt-5.4-mini", "exit_code": 0, "duration_seconds": 0.3, "prompt_chars": 800, "stdout_chars": 20, "stderr_chars": 10}],
            }
        )
    return {"release_id": "v1.2.3", "model_attempts": {"gpt-5.3-codex-spark": {"attempts": 1, "successful_attempts": 0, "failed_attempts": 1, "duration_seconds": 2.5, "prompt_chars": 2400, "stdout_chars": 25, "stderr_chars": 10}, "gpt-5.4-mini": {"attempts": 2, "successful_attempts": 2, "failed_attempts": 0, "duration_seconds": 1.1, "prompt_chars": 2600, "stdout_chars": 95, "stderr_chars": 50}}, "strong_model_calls": 3, "tasks": tasks}
