from __future__ import annotations

from agentic_devloop.models import TaskContract
from agentic_devloop.scientific import analyze_scientific_changes, benchmark_delta


def test_scientific_review_blocks_unapproved_fixture_and_tolerance_changes() -> None:
    task = _task()

    review = analyze_scientific_changes(
        task=task,
        changed_files=["tests/fixtures/expected.json"],
        diff_text="+rtol = 1e-3\n",
    )

    assert "Fixture-like files changed without explicit permission." in review.violations
    assert "Tolerance-like diff lines changed without explicit permission." in review.violations


def test_benchmark_delta_records_benchmark_like_changes() -> None:
    task = _task(task_type="benchmark", benchmark_delta_required=True)

    review = analyze_scientific_changes(
        task=task,
        changed_files=["benches/slope.rs"],
        diff_text="+new benchmark\n",
    )

    delta = benchmark_delta(task, review)

    assert delta["required"] is True
    assert delta["benchmark_changes"] == ["benches/slope.rs"]


def _task(task_type: str = "scientific_validation", benchmark_delta_required: bool = False) -> TaskContract:
    return TaskContract(
        task_id="sci-1",
        release_id="v1",
        title="Scientific task",
        task_type=task_type,
        budget_class="S",
        objective="Validate scientific behavior.",
        allowed_files=["tests/**", "benches/**"],
        required_evidence=["git diff"],
        verification={"commands": ["true"]},
        stop_conditions=["Verification fails twice."],
        scientific_assumptions=["No expected-value changes."],
        benchmark_delta_required=benchmark_delta_required,
    )
