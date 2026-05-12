from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from agentic_devloop.evidence import (
    EvidenceCollector,
    write_conflict_repair_result,
    write_failure_diagnosis,
    write_release_soft_gate_decisions,
    write_review_decision,
    write_task_soft_gate_decision,
)
from agentic_devloop.models import (
    ConflictRepairResult,
    ExecutorResult,
    FailureDiagnosis,
    FailureDiagnosisGuidance,
    FailureDiagnosisInput,
    FailureDiagnosisSourceMetadata,
    FailureEvidenceExcerpt,
    ReleaseSoftGateDecisionRecord,
    SoftGateDecision,
    SoftGateDecisionOutcome,
    SoftGateFinding,
    SoftGateSeverity,
    TaskSoftGateDecisionRecord,
    TaskContract,
    TaskRun,
    TaskState,
)
from agentic_devloop.review import deterministic_review
from agentic_devloop.yaml_io import load_yaml_model


ROOT = Path(__file__).resolve().parents[1]


def _git(repo, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_evidence_collector_writes_complete_bundle(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    (repo / "README.md").write_text("# changed\n", encoding="utf-8")

    task = load_yaml_model(ROOT / "contracts" / "rr-0001.yaml", TaskContract)
    now = datetime.now(timezone.utc)
    run_state = TaskRun(
        task_id=task.task_id,
        state=TaskState.REVIEWING,
        worktree_path=repo,
        branch="main",
        executor_attempts=1,
        started_at=now,
        updated_at=now,
        changed_files=["README.md"],
        diff_lines=2,
        verification_results=[],
    )

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    prompt_path = source_dir / "executor_prompt.md"
    stdout_path = source_dir / "executor_stdout.log"
    stderr_path = source_dir / "executor_stderr.log"
    verification_log_path = source_dir / "verification.log"
    prompt_path.write_text("Do the task.\n", encoding="utf-8")
    stdout_path.write_text("stdout\n", encoding="utf-8")
    stderr_path.write_text("stderr\n", encoding="utf-8")
    verification_log_path.write_text("verification\n", encoding="utf-8")

    executor_result = ExecutorResult(
        command=["codex"],
        exit_code=0,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        duration_seconds=1.0,
        backend="codex_cli",
        model="gpt-5.3-codex-spark",
    )

    bundle = EvidenceCollector().collect(
        run_id="2026-05-12_v0.8.0",
        task=task,
        run_state=run_state,
        worktree_path=repo,
        bundle_path=tmp_path / "runs" / "rr-0001",
        contract_source_path=ROOT / "contracts" / "rr-0001.yaml",
        executor_prompt_path=prompt_path,
        executor_result=executor_result,
        verification_log_path=verification_log_path,
    )

    assert bundle.contract_path.exists()
    assert bundle.run_state_path.exists()
    assert bundle.git_diff_path.read_text(encoding="utf-8")
    assert bundle.changed_files_path.read_text(encoding="utf-8") == "README.md\n"

    decision = deterministic_review(
        task=task,
        budget=task_budget(),
        changed_files=["tests/test_public_real_site_conditional_pilot_run.py"],
        diff_text="+assert report\n",
        verification_exit_codes=[0],
    )
    updated_bundle = write_review_decision(bundle, decision)

    assert updated_bundle.decision_path is not None
    assert updated_bundle.decision_path.exists()
    assert updated_bundle.review_path is not None
    assert updated_bundle.review_path.exists()

    repair_bundle = write_conflict_repair_result(
        updated_bundle,
        ConflictRepairResult(attempted=True, conflicted_files=["README.md"], resolved=False),
    )

    assert repair_bundle.conflict_repair_path is not None
    assert repair_bundle.conflict_repair_path.exists()


def test_write_failure_diagnosis_writes_yaml_bundle(tmp_path) -> None:
    bundle_root = tmp_path / "runs" / "rr-0001"
    bundle_root.mkdir(parents=True)
    bundle = load_bundle(bundle_root)

    diagnosis = FailureDiagnosis(
        diagnosis_inputs=[FailureDiagnosisInput(name="exit_code", value="1")],
        category="timeout",
        confidence=0.72,
        supporting_evidence_excerpts=[
            FailureEvidenceExcerpt(source="executor_stderr.log", excerpt="timed out after 600s")
        ],
        recommendation="Retry with a narrower contract and a longer walltime.",
        guidance=FailureDiagnosisGuidance(
            retryable=True,
            escalate=True,
            retry_reason="The task may succeed with a tighter scope.",
            escalate_reason="If retries continue to time out, human review is warranted.",
        ),
        source_metadata=FailureDiagnosisSourceMetadata(
            backend="codex_cli",
            model="gpt-5.3-codex-spark",
            command=["codex", "run"],
            exit_code=124,
            timed_out=True,
            stdout_path=bundle_root / "executor_stdout.log",
            stderr_path=bundle_root / "executor_stderr.log",
        ),
    )

    updated_bundle = write_failure_diagnosis(bundle, diagnosis)

    assert updated_bundle.failure_diagnosis_path is not None
    assert updated_bundle.failure_diagnosis_path.name == "failure_diagnosis.yaml"
    contents = updated_bundle.failure_diagnosis_path.read_text(encoding="utf-8")
    assert "category: timeout" in contents
    assert "retryable: true" in contents
    assert "timed_out: true" in contents


def test_write_failure_diagnosis_preserves_legacy_dict_payload(tmp_path) -> None:
    bundle_root = tmp_path / "runs" / "rr-0001"
    bundle_root.mkdir(parents=True)
    bundle = load_bundle(bundle_root)

    updated_bundle = write_failure_diagnosis(
        bundle,
        {
            "category": "executor_error",
            "recommendation": "Inspect logs and retry.",
            "final_exit_code": 1,
            "attempts": [
                {
                    "attempt": 1,
                    "model": "gpt-5.3-codex-spark",
                    "exit_code": 1,
                    "timed_out": False,
                }
            ],
        },
    )

    contents = updated_bundle.failure_diagnosis_path.read_text(encoding="utf-8")
    assert "final_exit_code: 1" in contents
    assert "attempts:" in contents
    assert "recommendation: Inspect logs and retry." in contents


def test_write_task_soft_gate_decision_writes_stable_json(tmp_path) -> None:
    bundle_root = tmp_path / "runs" / "rr-0001"
    bundle_root.mkdir(parents=True)
    bundle = load_bundle(bundle_root)
    record = TaskSoftGateDecisionRecord(
        task_id="rr-0001",
        finding=SoftGateFinding(
            finding_id="finding-001",
            severity=SoftGateSeverity.HIGH,
            risk="Potentially unsafe overlap if merged in parallel.",
            recommended_actions=["Sequence merge", "Rerun overlap validator"],
            evidence_paths=[bundle_root / "overlap_report.json"],
        ),
        decision=SoftGateDecision(
            finding_id="finding-001",
            decision=SoftGateDecisionOutcome.ACCEPT_WITH_MITIGATION,
            rationale="The overlap is manageable with serial merge ordering.",
            fallback_plan="Split task scope if overlap remains broad.",
            validators_rerun=["overlap_check", "admission_check"],
            evidence_paths=[bundle_root / "review_notes.md"],
        ),
    )

    updated_bundle = write_task_soft_gate_decision(bundle, record)
    assert updated_bundle.soft_gate_decision_path is not None
    payload = json.loads(updated_bundle.soft_gate_decision_path.read_text(encoding="utf-8"))

    assert payload["finding"]["finding_id"] == "finding-001"
    assert payload["finding"]["severity"] == "high"
    assert "overlap" in payload["finding"]["risk"]
    assert payload["decision"]["decision"] == "accept_with_mitigation"
    assert payload["decision"]["rationale"]
    assert payload["decision"]["fallback_plan"]
    assert payload["decision"]["validators_rerun"] == ["overlap_check", "admission_check"]
    assert payload["decision"]["evidence_paths"] == [str(bundle_root / "review_notes.md")]


def test_write_release_soft_gate_decisions_writes_stable_json(tmp_path) -> None:
    release_bundle_path = tmp_path / "runs" / "release-001"
    release_bundle_path.mkdir(parents=True)
    record = ReleaseSoftGateDecisionRecord(
        release_id="release-001",
        decisions=[
            TaskSoftGateDecisionRecord(
                task_id="rr-0001",
                finding=SoftGateFinding(
                    finding_id="finding-001",
                    severity=SoftGateSeverity.MODERATE,
                    risk="Minor budget overage risk.",
                    recommended_actions=["Accept with mitigation"],
                    evidence_paths=[release_bundle_path / "budget_report.json"],
                ),
                decision=SoftGateDecision(
                    finding_id="finding-001",
                    decision=SoftGateDecisionOutcome.ACCEPT,
                    rationale="Overage is within normal variance.",
                    fallback_plan="Escalate on repeated overage.",
                    validators_rerun=["budget_check"],
                    evidence_paths=[release_bundle_path / "review-rr-0001.md"],
                ),
            )
        ],
    )

    path = write_release_soft_gate_decisions(release_bundle_path, record)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert path.name == "soft_gate_decisions.json"
    assert payload["release_id"] == "release-001"
    assert payload["decisions"][0]["finding"]["finding_id"] == "finding-001"
    assert payload["decisions"][0]["finding"]["severity"] == "moderate"
    assert payload["decisions"][0]["decision"]["decision"] == "accept"
    assert payload["decisions"][0]["decision"]["fallback_plan"] == "Escalate on repeated overage."
    assert payload["decisions"][0]["decision"]["validators_rerun"] == ["budget_check"]


def task_budget():
    from agentic_devloop.models import Budget

    return Budget(
        max_executor_attempts_per_task=2,
        max_strong_model_calls_per_release=10,
        max_changed_files_per_task=8,
        max_diff_lines_per_task=600,
    )


def load_bundle(bundle_root: Path):
    from agentic_devloop.models import EvidenceBundle

    return EvidenceBundle(
        task_id="rr-0001",
        run_id="2026-05-12_v0.8.0",
        bundle_path=bundle_root,
        contract_path=bundle_root / "contract.yaml",
        run_state_path=bundle_root / "run_state.json",
        executor_prompt_path=bundle_root / "executor_prompt.md",
        executor_stdout_path=bundle_root / "executor_stdout.log",
        executor_stderr_path=bundle_root / "executor_stderr.log",
        git_diff_path=bundle_root / "git_diff.patch",
        changed_files_path=bundle_root / "changed_files.txt",
        verification_log_path=bundle_root / "verification.log",
    )
