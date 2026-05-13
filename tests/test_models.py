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
    ModelAvailability,
    ModelCatalogEntry,
    ProjectConfig,
    ReleaseObjective,
    ReviewDecision,
    Reviewer,
    FeatureReviewDecision,
    FeatureReviewFinding,
    FeatureReviewRecommendation,
    FeatureReviewRecheckRecord,
    FeatureReviewSeverity,
    SoftGateDecision,
    SoftGateDecisionOutcome,
    SoftGateFinding,
    SoftGateSeverity,
    TaskContract,
    TaskRun,
    TaskState,
)
from agentic_devloop.yaml_io import load_yaml_model


ROOT = Path(__file__).resolve().parents[1]


def test_sample_yaml_files_validate() -> None:
    project = load_yaml_model(ROOT / "configs" / "rust_rockfall.yaml", ProjectConfig)
    objective = ReleaseObjective(
        release_id="v0.8.0",
        title="Major feature release",
        objective="Implement a release-sized feature increment for rust_rockfall.",
        non_goals=["Do not weaken validation gates."],
        acceptance_criteria=["All default verification checks pass."],
    )
    contract = TaskContract(
        task_id="rr-0001",
        release_id="v0.8.0",
        title="Add regression test",
        task_type="scientific_validation",
        budget_class="M",
        objective="Add one bounded regression test.",
        allowed_files=["tests/**"],
        forbidden_changes=["Do not weaken assertions."],
        required_evidence=["git diff", "changed-files list"],
        verification={"commands": ["true"]},
        stop_conditions=["Stop if scope changes."],
        scientific_assumptions=["No scientific behavior changes are expected."],
    )

    assert project.project_id == "rust_rockfall"
    assert project.model_catalog["coding_worker"].model == "gpt-5.3-codex"
    assert project.model_catalog["micro_repair"].availability == ModelAvailability.UNKNOWN
    assert objective.release_id == "v0.8.0"
    assert contract.task_id == "rr-0001"


def test_project_config_accepts_model_catalog_and_legacy_configs() -> None:
    legacy = ProjectConfig.model_validate(
        {
            "project_id": "legacy",
            "repo_path": "/tmp/legacy",
            "default_base_branch": "main",
            "worktree_root": "/tmp/legacy-worktrees",
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
        }
    )
    modern = ProjectConfig.model_validate(
        legacy.model_dump(mode="python")
        | {
            "model_catalog": {
                "worker": ModelCatalogEntry(
                    model="gpt-5.3-codex",
                    capabilities=["implementation"],
                    budget_class="M",
                    availability=ModelAvailability.UNKNOWN,
                )
            }
        }
    )

    assert legacy.model_catalog == {}
    assert modern.model_catalog["worker"].capabilities == ["implementation"]


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


def test_soft_gate_models_validate_required_fields() -> None:
    finding = SoftGateFinding(
        finding_id="overlap-001",
        severity=SoftGateSeverity.MODERATE,
        risk="Potential merge conflict from file overlap.",
        recommended_actions=["Reorder tasks", "Rerun overlap validator"],
        evidence_paths=[Path("runs/demo/overlap_report.json")],
    )
    decision = SoftGateDecision(
        finding_id="overlap-001",
        decision=SoftGateDecisionOutcome.ACCEPT_WITH_MITIGATION,
        rationale="Overlap is narrow and mitigated by sequencing.",
        fallback_plan="Split the task if rerun still reports broad overlap.",
        validators_rerun=["overlap_check", "contract_scope_check"],
        evidence_paths=[Path("runs/demo/soft_gate_review.md")],
    )

    assert finding.finding_id == "overlap-001"
    assert decision.decision == SoftGateDecisionOutcome.ACCEPT_WITH_MITIGATION


def test_soft_gate_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SoftGateFinding.model_validate(
            {
                "finding_id": "budget-001",
                "severity": "low",
                "risk": "Small overage.",
                "recommended_actions": ["Track overage."],
                "unexpected_field": True,
            }
        )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SoftGateDecision.model_validate(
            {
                "finding_id": "budget-001",
                "decision": "accept",
                "rationale": "Within expected variance.",
                "fallback_plan": "Escalate if repeated.",
                "validators_rerun": ["budget_check"],
                "unknown": "value",
            }
        )


def test_feature_review_models_validate() -> None:
    decision = FeatureReviewDecision(
        release_id="release-001",
        reviewer=Reviewer.STRONG_MODEL,
        summary="Integrated feature branch is mostly sound with one required repair.",
        findings=[
            FeatureReviewFinding(
                finding_id="feature-001",
                severity=FeatureReviewSeverity.HIGH,
                summary="Missing negative-path verification for release finalization guard.",
                affected_files=["src/agentic_devloop/release.py", "tests/test_release.py"],
                evidence_paths=[Path("runs/release-001/release_summary.json")],
                required_repairs=["Add regression test for guard failure path."],
                optional_follow_ups=["Add reviewer checklist coverage for guard outcomes."],
            )
        ],
        accepted_risks=["Minor docs drift accepted for this release increment."],
        recommendation=FeatureReviewRecommendation.APPROVE_WITH_REPAIRS,
        rerun_verification_commands=["pytest tests/test_release.py"],
    )
    recheck = FeatureReviewRecheckRecord(
        release_id="release-001",
        unresolved_finding_ids=["feature-001"],
        resolved_finding_ids=[],
        accepted_finding_ids=[],
        stop_reason="blocked_by_hard_gate",
    )

    assert decision.findings[0].severity == FeatureReviewSeverity.HIGH
    assert decision.recommendation == FeatureReviewRecommendation.APPROVE_WITH_REPAIRS
    assert recheck.stop_reason == "blocked_by_hard_gate"


def test_feature_review_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FeatureReviewFinding.model_validate(
            {
                "finding_id": "feature-001",
                "severity": "high",
                "summary": "Risky change.",
                "unexpected": True,
            }
        )


def test_feature_review_findings_with_actions_require_affected_files() -> None:
    with pytest.raises(ValidationError, match="require affected_files"):
        FeatureReviewFinding.model_validate(
            {
                "finding_id": "feature-001",
                "severity": "high",
                "summary": "Required repair lacks scope.",
                "required_repairs": ["Fix the issue."],
            }
        )

    with pytest.raises(ValidationError, match="require affected_files"):
        FeatureReviewFinding.model_validate(
            {
                "finding_id": "feature-002",
                "severity": "moderate",
                "summary": "Optional follow-up lacks scope.",
                "optional_follow_ups": ["Document the follow-up."],
            }
        )


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
