from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from agentic_devloop.models import TaskContract, TaskType


FIXTURE_PATTERNS = ("fixture", "fixtures", "golden", "snapshot", "expected")
BENCHMARK_PATTERNS = ("bench", "benchmark", "calibration")
TOLERANCE_PATTERN = re.compile(r"\b(atol|rtol|epsilon|eps|tolerance|tol)\b", re.IGNORECASE)


@dataclass(frozen=True)
class ScientificReview:
    fixture_changes: list[str]
    tolerance_changes: list[str]
    benchmark_changes: list[str]
    violations: list[str]

    @property
    def has_findings(self) -> bool:
        return bool(
            self.fixture_changes
            or self.tolerance_changes
            or self.benchmark_changes
            or self.violations
        )


def analyze_scientific_changes(
    *,
    task: TaskContract,
    changed_files: list[str],
    diff_text: str,
) -> ScientificReview:
    fixture_changes = [path for path in changed_files if _matches(path, FIXTURE_PATTERNS)]
    benchmark_changes = [path for path in changed_files if _matches(path, BENCHMARK_PATTERNS)]
    tolerance_changes = _tolerance_lines(diff_text)
    violations: list[str] = []

    if fixture_changes and not task.fixture_changes_allowed:
        violations.append("Fixture-like files changed without explicit permission.")
    if tolerance_changes and not task.tolerance_changes_allowed:
        violations.append("Tolerance-like diff lines changed without explicit permission.")
    if task.task_type in {TaskType.SCIENTIFIC_VALIDATION, TaskType.VALIDATION} and not task.validation_assumptions:
        violations.append("Validation task has no recorded validation assumptions.")

    return ScientificReview(
        fixture_changes=fixture_changes,
        tolerance_changes=tolerance_changes,
        benchmark_changes=benchmark_changes,
        violations=violations,
    )


def benchmark_delta(task: TaskContract, review: ScientificReview) -> dict:
    return {
        "required": task.benchmark_delta_required or task.task_type == TaskType.BENCHMARK,
        "benchmark_changes": review.benchmark_changes,
        "note": "Benchmark execution is not implemented; this records changed benchmark-like files.",
    }


def write_scientific_review(path: Path, review: ScientificReview) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "fixture_changes": review.fixture_changes,
                "tolerance_changes": review.tolerance_changes,
                "benchmark_changes": review.benchmark_changes,
                "violations": review.violations,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    lowered = path.lower()
    return any(pattern in lowered for pattern in patterns)


def _tolerance_lines(diff_text: str) -> list[str]:
    lines = []
    for line in diff_text.splitlines():
        if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
            continue
        if TOLERANCE_PATTERN.search(line):
            lines.append(line)
    return lines
