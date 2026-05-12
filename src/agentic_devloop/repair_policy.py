from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RepairFailureCategory(StrEnum):
    PLANNER_SCHEMA_DRIFT = "planner_schema_drift"
    VERIFICATION_ENVIRONMENT_DRIFT = "verification_environment_drift"
    FLAKY_VERIFICATION = "flaky_verification"
    CONTRACT_CONTAINED_WORKER_FAILURE = "contract_contained_worker_failure"
    CONTRACT_BOUNDARY_VIOLATION = "contract_boundary_violation"
    MISSING_CREDENTIALS = "missing_credentials"
    UNSAFE_POLICY_EXPANSION = "unsafe_policy_expansion"
    EXHAUSTED_RETRY_BUDGET = "exhausted_retry_budget"


class RepairDecisionKind(StrEnum):
    RETRY = "retry"
    STOP = "stop"


class RepairStopReason(StrEnum):
    EXHAUSTED_RETRY_BUDGET = "exhausted_retry_budget"
    CONTRACT_BOUNDARY_VIOLATION = "contract_boundary_violation"
    MISSING_CREDENTIALS = "missing_credentials"
    UNSAFE_POLICY_EXPANSION = "unsafe_policy_expansion"


@dataclass(frozen=True)
class RepairDecision:
    category: RepairFailureCategory
    decision: RepairDecisionKind
    retryable: bool
    reason: str
    attempt: int
    max_retries: int
    remaining_retries: int
    stop_reason: RepairStopReason | None = None


def decide_repair_policy(
    category: RepairFailureCategory,
    *,
    attempt: int,
    max_retries: int,
) -> RepairDecision:
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    if max_retries < 0:
        raise ValueError("max_retries must be >= 0")

    remaining_retries = max(max_retries - attempt, 0)

    if category == RepairFailureCategory.EXHAUSTED_RETRY_BUDGET:
        return RepairDecision(
            category=category,
            decision=RepairDecisionKind.STOP,
            retryable=False,
            reason="Autonomous repair budget is exhausted.",
            attempt=attempt,
            max_retries=max_retries,
            remaining_retries=0,
            stop_reason=RepairStopReason.EXHAUSTED_RETRY_BUDGET,
        )

    if category == RepairFailureCategory.CONTRACT_BOUNDARY_VIOLATION:
        return RepairDecision(
            category=category,
            decision=RepairDecisionKind.STOP,
            retryable=False,
            reason="Failure crossed the task contract boundary and requires escalation.",
            attempt=attempt,
            max_retries=max_retries,
            remaining_retries=remaining_retries,
            stop_reason=RepairStopReason.CONTRACT_BOUNDARY_VIOLATION,
        )

    if category == RepairFailureCategory.MISSING_CREDENTIALS:
        return RepairDecision(
            category=category,
            decision=RepairDecisionKind.STOP,
            retryable=False,
            reason="Required credentials are missing and must be provided externally.",
            attempt=attempt,
            max_retries=max_retries,
            remaining_retries=remaining_retries,
            stop_reason=RepairStopReason.MISSING_CREDENTIALS,
        )

    if category == RepairFailureCategory.UNSAFE_POLICY_EXPANSION:
        return RepairDecision(
            category=category,
            decision=RepairDecisionKind.STOP,
            retryable=False,
            reason="Proposed repair would expand policy unsafely beyond configured boundaries.",
            attempt=attempt,
            max_retries=max_retries,
            remaining_retries=remaining_retries,
            stop_reason=RepairStopReason.UNSAFE_POLICY_EXPANSION,
        )

    retryable_categories = {
        RepairFailureCategory.PLANNER_SCHEMA_DRIFT,
        RepairFailureCategory.VERIFICATION_ENVIRONMENT_DRIFT,
        RepairFailureCategory.FLAKY_VERIFICATION,
        RepairFailureCategory.CONTRACT_CONTAINED_WORKER_FAILURE,
    }
    if category in retryable_categories:
        if attempt < max_retries:
            return RepairDecision(
                category=category,
                decision=RepairDecisionKind.RETRY,
                retryable=True,
                reason="Failure is contract-contained and retry is allowed within the repair budget.",
                attempt=attempt,
                max_retries=max_retries,
                remaining_retries=remaining_retries,
            )
        return RepairDecision(
            category=category,
            decision=RepairDecisionKind.STOP,
            retryable=False,
            reason="Retry budget exhausted for a contract-contained failure.",
            attempt=attempt,
            max_retries=max_retries,
            remaining_retries=0,
            stop_reason=RepairStopReason.EXHAUSTED_RETRY_BUDGET,
        )

    # Exhaustiveness guard for future enum additions.
    raise ValueError(f"Unhandled repair failure category: {category}")
