from __future__ import annotations

from fnmatch import fnmatch

from agentic_devloop.models import (
    Budget,
    Decision,
    ReviewDecision,
    Reviewer,
    SoftGateFinding,
    SoftGateSeverity,
    TaskContract,
)
from agentic_devloop.scientific import ScientificReview

SOFT_BUDGET_MINOR_OVERAGE_SHARE = 0.10
SOFT_BUDGET_MODERATE_OVERAGE_SHARE = 0.25


def _diff_line_count(diff_text: str) -> int:
    return sum(1 for line in diff_text.splitlines() if line.startswith(("+", "-")))


def _is_allowed(path: str, allowed_patterns: list[str]) -> bool:
    return any(fnmatch(path, pattern) for pattern in allowed_patterns)


def _budget_overage_severity(*, actual: int, limit: int) -> SoftGateSeverity:
    overage_share = (actual - limit) / limit
    if overage_share <= SOFT_BUDGET_MINOR_OVERAGE_SHARE:
        return SoftGateSeverity.LOW
    if overage_share <= SOFT_BUDGET_MODERATE_OVERAGE_SHARE:
        return SoftGateSeverity.MODERATE
    return SoftGateSeverity.HIGH


def deterministic_review(
    *,
    task: TaskContract,
    budget: Budget,
    changed_files: list[str],
    diff_text: str,
    verification_exit_codes: list[int],
    scientific_review: ScientificReview | None = None,
) -> ReviewDecision:
    risks: list[str] = []
    soft_gate_findings: list[SoftGateFinding] = []

    if any(exit_code != 0 for exit_code in verification_exit_codes):
        return ReviewDecision(
            task_id=task.task_id,
            decision=Decision.FAILED,
            reviewer=Reviewer.DETERMINISTIC,
            rationale="Verification failed.",
        )

    disallowed_files = [
        changed_file for changed_file in changed_files if not _is_allowed(changed_file, task.allowed_files)
    ]
    if disallowed_files:
        return ReviewDecision(
            task_id=task.task_id,
            decision=Decision.NEEDS_REVISION,
            reviewer=Reviewer.DETERMINISTIC,
            rationale=f"Changed files outside allowed paths: {', '.join(disallowed_files)}.",
        )

    if scientific_review is not None and scientific_review.violations:
        return ReviewDecision(
            task_id=task.task_id,
            decision=Decision.NEEDS_REVISION,
            reviewer=Reviewer.DETERMINISTIC,
            rationale="Scientific review violations: " + "; ".join(scientific_review.violations),
        )

    changed_files_count = len(changed_files)
    if changed_files_count > budget.max_changed_files_per_task:
        severity = _budget_overage_severity(
            actual=changed_files_count,
            limit=budget.max_changed_files_per_task,
        )
        soft_gate_findings.append(
            SoftGateFinding(
                finding_id=f"{task.task_id}:changed_files_budget",
                severity=severity,
                risk=(
                    f"Scope-risk changed-files overage: {changed_files_count} changed files "
                    f"over budget: {changed_files_count} exceeds {budget.max_changed_files_per_task}."
                ),
                recommended_actions=[
                    "Review whether task scope should be split.",
                    "Confirm changed files remain inside allowed contract paths.",
                ],
            )
        )

    diff_lines = _diff_line_count(diff_text)
    if diff_lines > budget.max_diff_lines_per_task:
        severity = _budget_overage_severity(
            actual=diff_lines,
            limit=budget.max_diff_lines_per_task,
        )
        soft_gate_findings.append(
            SoftGateFinding(
                finding_id=f"{task.task_id}:diff_lines_budget",
                severity=severity,
                risk=(
                    f"Scope-risk diff-size overage: {diff_lines} diff lines "
                    f"over budget: {diff_lines} exceeds {budget.max_diff_lines_per_task}."
                ),
                recommended_actions=[
                    "Review whether task scope should be split.",
                    "Re-run verification after scope reduction if changes are deferred.",
                ],
            )
        )

    if not changed_files:
        risks.append("No changed files were detected.")

    if soft_gate_findings:
        risks.append(
            "Task exceeded one or more size budgets; a scope-risk reviewer/supervisor soft-gate decision is required."
        )

    return ReviewDecision(
        task_id=task.task_id,
        decision=Decision.ACCEPTED,
        reviewer=Reviewer.DETERMINISTIC,
        rationale="Verification passed and diff is within contract.",
        risks=risks,
        soft_gate_findings=soft_gate_findings,
    )
