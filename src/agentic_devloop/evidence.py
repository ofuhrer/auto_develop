from __future__ import annotations

import json
import shutil
from pathlib import Path

from agentic_devloop.git_finalize import FinalizeResult
from agentic_devloop.git_state import changed_files, diff_patch
from agentic_devloop.models import (
    ConflictRepairResult,
    EvidenceBundle,
    FailureDiagnosis,
    ExecutorResult,
    FeatureReviewDecision,
    FeatureReviewRecheckRecord,
    ReleaseSoftGateDecisionRecord,
    ReviewDecision,
    TaskSoftGateDecisionRecord,
    TaskContract,
    TaskRun,
)
from agentic_devloop.scientific import ScientificReview, benchmark_delta


def _normalized_string_list(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item:
            continue
        normalized.append(item)
    return normalized


def supervisor_decisions_artifacts_dir(release_bundle_path: Path) -> Path:
    return release_bundle_path / "supervisor_decisions"


class EvidenceCollector:
    def collect(
        self,
        *,
        run_id: str,
        task: TaskContract,
        run_state: TaskRun,
        worktree_path: Path,
        bundle_path: Path,
        contract_source_path: Path,
        executor_prompt_path: Path,
        executor_result: ExecutorResult,
        verification_log_path: Path,
    ) -> EvidenceBundle:
        if bundle_path.exists() and any(bundle_path.iterdir()):
            raise FileExistsError(f"evidence bundle already exists: {bundle_path}")

        bundle_path.mkdir(parents=True, exist_ok=False)

        contract_path = bundle_path / "contract.yaml"
        run_state_path = bundle_path / "run_state.json"
        prompt_path = bundle_path / "executor_prompt.md"
        executor_stdout_path = bundle_path / "executor_stdout.log"
        executor_stderr_path = bundle_path / "executor_stderr.log"
        git_diff_path = bundle_path / "git_diff.patch"
        changed_files_path = bundle_path / "changed_files.txt"
        verification_bundle_log_path = bundle_path / "verification.log"
        model_call_metadata_path = bundle_path / "model_call_metadata.json"
        executor_attempts_path = bundle_path / "executor_attempts.json"

        shutil.copyfile(contract_source_path, contract_path)
        shutil.copyfile(executor_prompt_path, prompt_path)
        shutil.copyfile(executor_result.stdout_path, executor_stdout_path)
        shutil.copyfile(executor_result.stderr_path, executor_stderr_path)
        shutil.copyfile(verification_log_path, verification_bundle_log_path)

        run_state_path.write_text(
            json.dumps(run_state.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        model_call_metadata_path.write_text(
            json.dumps(
                {
                    "backend": executor_result.backend,
                    "model": executor_result.model,
                    "command": executor_result.command,
                    "exit_code": executor_result.exit_code,
                    "duration_seconds": executor_result.duration_seconds,
                    "timed_out": executor_result.timed_out,
                    "prompt_chars": executor_result.prompt_chars,
                    "stdout_chars": executor_result.stdout_chars,
                    "stderr_chars": executor_result.stderr_chars,
                    "approx_output_chars": executor_result.stdout_chars + executor_result.stderr_chars,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        executor_attempts_path.write_text(
            json.dumps(
                [
                    {
                        **attempt.model_dump(mode="json"),
                        "approx_output_chars": attempt.stdout_chars + attempt.stderr_chars,
                    }
                    for attempt in executor_result.attempts
                ],
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        git_diff_path.write_text(diff_patch(worktree_path), encoding="utf-8")
        changed_files_path.write_text(
            "\n".join(changed_files(worktree_path)) + "\n",
            encoding="utf-8",
        )

        return EvidenceBundle(
            task_id=task.task_id,
            run_id=run_id,
            bundle_path=bundle_path,
            contract_path=contract_path,
            run_state_path=run_state_path,
            executor_prompt_path=prompt_path,
            executor_stdout_path=executor_stdout_path,
            executor_stderr_path=executor_stderr_path,
            git_diff_path=git_diff_path,
            changed_files_path=changed_files_path,
            verification_log_path=verification_bundle_log_path,
            model_call_metadata_path=model_call_metadata_path,
            executor_attempts_path=executor_attempts_path,
        )


def write_review_decision(bundle: EvidenceBundle, decision: ReviewDecision) -> EvidenceBundle:
    decision_path = bundle.bundle_path / "decision.yaml"
    review_path = bundle.bundle_path / "review.md"

    decision_path.write_text(
        _decision_yaml(decision),
        encoding="utf-8",
    )
    review_path.write_text(
        "\n".join(
            [
                f"# Review: {decision.task_id}",
                "",
                f"- Decision: `{decision.decision}`",
                f"- Reviewer: `{decision.reviewer}`",
                f"- Rationale: {decision.rationale}",
                "",
                "## Risks",
                "",
                *[f"- {risk}" for risk in decision.risks],
                *(["- None recorded."] if not decision.risks else []),
                "",
                "## Follow-up Tasks",
                "",
                *[f"- {task}" for task in decision.follow_up_tasks],
                *(["- None recorded."] if not decision.follow_up_tasks else []),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return bundle.model_copy(update={"decision_path": decision_path, "review_path": review_path})


def write_finalization_result(bundle: EvidenceBundle, result: FinalizeResult) -> EvidenceBundle:
    finalization_path = bundle.bundle_path / "finalization.yaml"
    finalization_path.write_text(
        _yaml(result.__dict__),
        encoding="utf-8",
    )
    return bundle.model_copy(update={"finalization_path": finalization_path})


def write_conflict_repair_result(bundle: EvidenceBundle, result: ConflictRepairResult) -> EvidenceBundle:
    conflict_repair_path = bundle.bundle_path / "conflict_repair.yaml"
    conflict_repair_path.write_text(_yaml(result.model_dump(mode="json")), encoding="utf-8")
    return bundle.model_copy(update={"conflict_repair_path": conflict_repair_path})


def write_failure_diagnosis(bundle: EvidenceBundle, diagnosis: FailureDiagnosis | dict) -> EvidenceBundle:
    failure_diagnosis_path = bundle.bundle_path / "failure_diagnosis.yaml"
    if isinstance(diagnosis, FailureDiagnosis):
        payload = diagnosis.model_dump(mode="json")
    else:
        payload = diagnosis
    failure_diagnosis_path.write_text(_yaml(payload), encoding="utf-8")
    return bundle.model_copy(update={"failure_diagnosis_path": failure_diagnosis_path})


def write_scientific_outputs(
    bundle: EvidenceBundle,
    task: TaskContract,
    review: ScientificReview,
) -> EvidenceBundle:
    from agentic_devloop.scientific import write_scientific_review

    scientific_review_path = write_scientific_review(bundle.bundle_path / "scientific_review.yaml", review)
    updates = {"scientific_review_path": scientific_review_path}
    delta = benchmark_delta(task, review)
    if delta["required"] or delta["benchmark_changes"]:
        benchmark_delta_path = bundle.bundle_path / "benchmark_delta.json"
        import json

        benchmark_delta_path.write_text(json.dumps(delta, indent=2) + "\n", encoding="utf-8")
        updates["benchmark_delta_path"] = benchmark_delta_path
    if task.remote_dispatch is not None:
        remote_dispatch_path = bundle.bundle_path / "remote_dispatch.yaml"
        remote_dispatch_path.write_text(
            _yaml(
                {
                    **task.remote_dispatch.model_dump(mode="json"),
                    "status": "declared_not_executed",
                    "note": "Remote dispatch metadata is recorded; execution backend is not implemented.",
                }
            ),
            encoding="utf-8",
        )
        updates["remote_dispatch_path"] = remote_dispatch_path

    return bundle.model_copy(update=updates)


def write_task_soft_gate_decision(
    bundle: EvidenceBundle,
    record: TaskSoftGateDecisionRecord,
) -> EvidenceBundle:
    soft_gate_decision_path = bundle.bundle_path / "soft_gate_decision.json"
    payload = {
        "task_id": record.task_id,
        "finding": {
            "finding_id": record.finding.finding_id,
            "severity": record.finding.severity.value,
            "risk": record.finding.risk,
            "recommended_actions": record.finding.recommended_actions,
            "evidence_paths": [str(path) for path in record.finding.evidence_paths],
        },
        "decision": {
            "finding_id": record.decision.finding_id,
            "decision": record.decision.decision.value,
            "rationale": record.decision.rationale,
            "fallback_plan": record.decision.fallback_plan,
            "validators_rerun": record.decision.validators_rerun,
            "evidence_paths": [str(path) for path in record.decision.evidence_paths],
        },
    }
    soft_gate_decision_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return bundle.model_copy(update={"soft_gate_decision_path": soft_gate_decision_path})


def write_release_soft_gate_decisions(
    release_bundle_path: Path,
    record: ReleaseSoftGateDecisionRecord,
) -> Path:
    release_soft_gate_path = release_bundle_path / "soft_gate_decisions.json"
    payload = {
        "release_id": record.release_id,
        "decisions": [
            {
                "task_id": decision.task_id,
                "finding": {
                    "finding_id": decision.finding.finding_id,
                    "severity": decision.finding.severity.value,
                    "risk": decision.finding.risk,
                    "recommended_actions": decision.finding.recommended_actions,
                    "evidence_paths": [str(path) for path in decision.finding.evidence_paths],
                },
                "decision": {
                    "finding_id": decision.decision.finding_id,
                    "decision": decision.decision.decision.value,
                    "rationale": decision.decision.rationale,
                    "fallback_plan": decision.decision.fallback_plan,
                    "validators_rerun": decision.decision.validators_rerun,
                    "evidence_paths": [str(path) for path in decision.decision.evidence_paths],
                },
            }
            for decision in record.decisions
        ],
    }
    release_soft_gate_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return release_soft_gate_path


def write_feature_review_decision(
    release_bundle_path: Path,
    decision: FeatureReviewDecision,
) -> Path:
    feature_review_path = release_bundle_path / "feature_review.json"
    payload = {
        "release_id": decision.release_id,
        "reviewer": decision.reviewer.value,
        "summary": decision.summary,
        "recommendation": decision.recommendation.value,
        "accepted_risks": _normalized_string_list(decision.accepted_risks),
        "rerun_verification_commands": _normalized_string_list(decision.rerun_verification_commands),
        "findings": [
            {
                "finding_id": finding.finding_id,
                "severity": finding.severity.value,
                "summary": finding.summary,
                "affected_files": _normalized_string_list(finding.affected_files),
                "evidence_paths": [str(path) for path in finding.evidence_paths],
                "required_repairs": _normalized_string_list(finding.required_repairs),
                "optional_follow_ups": _normalized_string_list(finding.optional_follow_ups),
            }
            for finding in decision.findings
        ],
    }
    feature_review_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return feature_review_path


def write_feature_review_recheck(
    release_bundle_path: Path,
    record: FeatureReviewRecheckRecord,
) -> Path:
    feature_review_recheck_path = release_bundle_path / "feature_review_recheck.json"
    payload = {
        "release_id": record.release_id,
        "unresolved_finding_ids": _normalized_string_list(record.unresolved_finding_ids),
        "resolved_finding_ids": _normalized_string_list(record.resolved_finding_ids),
        "accepted_finding_ids": _normalized_string_list(record.accepted_finding_ids),
        "deferred_finding_ids": _normalized_string_list(record.deferred_finding_ids),
        "stop_reason": record.stop_reason,
    }
    feature_review_recheck_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return feature_review_recheck_path


def _decision_yaml(decision: ReviewDecision) -> str:
    return _yaml(decision.model_dump(mode="json"))


def _yaml(data: dict) -> str:
    import yaml

    return yaml.safe_dump(data, sort_keys=False)
