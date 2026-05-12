from __future__ import annotations

import pytest

from agentic_devloop.repair_policy import (
    RepairDecisionKind,
    RepairFailureCategory,
    RepairStopReason,
    decide_repair_policy,
)


@pytest.mark.parametrize(
    "category",
    [
        RepairFailureCategory.PLANNER_SCHEMA_DRIFT,
        RepairFailureCategory.VERIFICATION_ENVIRONMENT_DRIFT,
        RepairFailureCategory.FLAKY_VERIFICATION,
        RepairFailureCategory.CONTRACT_CONTAINED_WORKER_FAILURE,
    ],
)
def test_contract_contained_failures_retry_within_budget(category: RepairFailureCategory) -> None:
    decision = decide_repair_policy(category, attempt=1, max_retries=3)

    assert decision.decision == RepairDecisionKind.RETRY
    assert decision.retryable is True
    assert decision.stop_reason is None
    assert decision.remaining_retries == 2


@pytest.mark.parametrize(
    "category",
    [
        RepairFailureCategory.PLANNER_SCHEMA_DRIFT,
        RepairFailureCategory.VERIFICATION_ENVIRONMENT_DRIFT,
        RepairFailureCategory.FLAKY_VERIFICATION,
        RepairFailureCategory.CONTRACT_CONTAINED_WORKER_FAILURE,
    ],
)
def test_contract_contained_failures_stop_when_retry_budget_exhausted(category: RepairFailureCategory) -> None:
    decision = decide_repair_policy(category, attempt=3, max_retries=3)

    assert decision.decision == RepairDecisionKind.STOP
    assert decision.retryable is False
    assert decision.stop_reason == RepairStopReason.EXHAUSTED_RETRY_BUDGET
    assert decision.remaining_retries == 0


@pytest.mark.parametrize(
    "category,expected_stop_reason",
    [
        (RepairFailureCategory.CONTRACT_BOUNDARY_VIOLATION, RepairStopReason.CONTRACT_BOUNDARY_VIOLATION),
        (RepairFailureCategory.MISSING_CREDENTIALS, RepairStopReason.MISSING_CREDENTIALS),
        (RepairFailureCategory.UNSAFE_POLICY_EXPANSION, RepairStopReason.UNSAFE_POLICY_EXPANSION),
        (RepairFailureCategory.EXHAUSTED_RETRY_BUDGET, RepairStopReason.EXHAUSTED_RETRY_BUDGET),
    ],
)
def test_stop_classification_for_non_retryable_categories(
    category: RepairFailureCategory,
    expected_stop_reason: RepairStopReason,
) -> None:
    decision = decide_repair_policy(category, attempt=1, max_retries=3)

    assert decision.decision == RepairDecisionKind.STOP
    assert decision.retryable is False
    assert decision.stop_reason == expected_stop_reason


@pytest.mark.parametrize(
    "attempt,max_retries",
    [
        (0, 1),
        (1, -1),
    ],
)
def test_invalid_decision_inputs_raise(attempt: int, max_retries: int) -> None:
    with pytest.raises(ValueError):
        decide_repair_policy(
            RepairFailureCategory.CONTRACT_CONTAINED_WORKER_FAILURE,
            attempt=attempt,
            max_retries=max_retries,
        )
