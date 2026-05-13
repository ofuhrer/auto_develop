from __future__ import annotations

from agentic_devloop.models import Budget, Decision, SoftGateSeverity, TaskContract
from agentic_devloop.review import deterministic_review


def test_deterministic_review_accepts_in_contract_change() -> None:
    task = _task_contract()
    budget = Budget(
        max_executor_attempts_per_task=2,
        max_strong_model_calls_per_release=10,
        max_changed_files_per_task=8,
        max_diff_lines_per_task=600,
    )

    decision = deterministic_review(
        task=task,
        budget=budget,
        changed_files=["tests/test_public_real_site_conditional_pilot_run.py"],
        diff_text="+assert report\n",
        verification_exit_codes=[0],
    )

    assert decision.decision == Decision.ACCEPTED


def test_deterministic_review_rejects_disallowed_file() -> None:
    task = _task_contract()
    budget = Budget(
        max_executor_attempts_per_task=2,
        max_strong_model_calls_per_release=10,
        max_changed_files_per_task=8,
        max_diff_lines_per_task=600,
    )

    decision = deterministic_review(
        task=task,
        budget=budget,
        changed_files=["src/lib.rs"],
        diff_text="+change\n",
        verification_exit_codes=[0],
    )

    assert decision.decision == Decision.NEEDS_REVISION


def test_deterministic_review_classifies_minor_budget_overage_as_soft_finding() -> None:
    task = _task_contract()
    budget = Budget(
        max_executor_attempts_per_task=2,
        max_strong_model_calls_per_release=10,
        max_changed_files_per_task=10,
        max_diff_lines_per_task=10,
    )

    decision = deterministic_review(
        task=task,
        budget=budget,
        changed_files=[
            "tests/test_public_real_site_conditional_pilot_run.py"
            for _ in range(11)
        ],
        diff_text="+line\n",
        verification_exit_codes=[0],
    )

    assert decision.decision == Decision.ACCEPTED
    assert len(decision.soft_gate_findings) == 1
    assert decision.soft_gate_findings[0].severity == SoftGateSeverity.LOW
    assert (
        decision.soft_gate_findings[0].risk
        == "Scope-risk changed-files overage: over budget: 11 exceeds 10 changed files."
    )


def _task_contract() -> TaskContract:
    return TaskContract(
        task_id="rr-0001",
        release_id="v0.8.0",
        title="Add regression test for selected validation gate report mismatch",
        task_type="scientific_validation",
        budget_class="M",
        objective="Add one regression test covering the mismatch between selected gate evidence and generated report output.",
        allowed_files=["tests/**", "scripts/validate_public_real_site_conditional_pilot_run.py"],
        forbidden_changes=["Do not change validation schema."],
        required_evidence=["git diff", "test output", "changed-files list"],
        verification={"commands": ["true"]},
        stop_conditions=["Stop if scope changes."],
        scientific_assumptions=["No scientific behavior changes are expected."],
    )
