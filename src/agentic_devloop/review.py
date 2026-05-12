from __future__ import annotations

from fnmatch import fnmatch

from agentic_devloop.models import Budget, Decision, ReviewDecision, Reviewer, TaskContract
from agentic_devloop.scientific import ScientificReview


def _diff_line_count(diff_text: str) -> int:
    return sum(1 for line in diff_text.splitlines() if line.startswith(("+", "-")))


def _is_allowed(path: str, allowed_patterns: list[str]) -> bool:
    return any(fnmatch(path, pattern) for pattern in allowed_patterns)


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

    if len(changed_files) > budget.max_changed_files_per_task:
        return ReviewDecision(
            task_id=task.task_id,
            decision=Decision.NEEDS_REVISION,
            reviewer=Reviewer.DETERMINISTIC,
            rationale="Changed file count exceeds task budget.",
        )

    diff_lines = _diff_line_count(diff_text)
    if diff_lines > budget.max_diff_lines_per_task:
        return ReviewDecision(
            task_id=task.task_id,
            decision=Decision.NEEDS_REVISION,
            reviewer=Reviewer.DETERMINISTIC,
            rationale="Diff line count exceeds task budget.",
        )

    if not changed_files:
        risks.append("No changed files were detected.")

    return ReviewDecision(
        task_id=task.task_id,
        decision=Decision.ACCEPTED,
        reviewer=Reviewer.DETERMINISTIC,
        rationale="Verification passed and diff is within contract.",
        risks=risks,
    )
