from __future__ import annotations

from pathlib import Path

from agentic_devloop.models import Budget, Decision, SoftGateSeverity, TaskContract
from agentic_devloop.review import deterministic_review
from agentic_devloop.yaml_io import load_yaml_model


ROOT = Path(__file__).resolve().parents[1]


def test_deterministic_review_accepts_in_contract_change() -> None:
    task = load_yaml_model(ROOT / "contracts" / "rr-0001.yaml", TaskContract)
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
    task = load_yaml_model(ROOT / "contracts" / "rr-0001.yaml", TaskContract)
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
    task = load_yaml_model(ROOT / "contracts" / "rr-0001.yaml", TaskContract)
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
