from __future__ import annotations

from pathlib import Path

from agentic_devloop.models import ContextBundle, ContextSection, ProjectConfig, TaskContract
from agentic_devloop.security import redact_text


STATE_FILES = [
    ("architecture_summary", "architecture_summary.md"),
    ("active_constraints", "active_constraints.yaml"),
    ("benchmark_status", "benchmark_status.json"),
    ("known_failures", "known_failures.md"),
    ("release_plan", "release_plan.yaml"),
]


class ContextBudgetError(ValueError):
    pass


def load_context_bundle(config: ProjectConfig, task: TaskContract) -> ContextBundle:
    if config.repo_state_path is None:
        return ContextBundle()

    root = _resolve_state_path(config)
    sections: list[ContextSection] = []
    for name, filename in STATE_FILES:
        path = root / filename
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            continue
        sections.append(ContextSection(name=name, source_path=path, content=redact_text(content)))

    return _select_relevant_context(ContextBundle(sections=sections), task)


def enforce_context_budget(context: ContextBundle, max_chars: int) -> None:
    if context.total_chars > max_chars:
        raise ContextBudgetError(
            f"context bundle has {context.total_chars} chars, exceeding budget {max_chars}"
        )


def _resolve_state_path(config: ProjectConfig) -> Path:
    assert config.repo_state_path is not None
    if config.repo_state_path.is_absolute():
        return config.repo_state_path
    return config.repo_path / config.repo_state_path


def _select_relevant_context(context: ContextBundle, task: TaskContract) -> ContextBundle:
    # v1 retrieval is intentionally conservative: always include compact canonical
    # state files, but only include known failures when they mention this task or release.
    selected: list[ContextSection] = []
    failure_terms = {task.task_id.lower(), task.release_id.lower(), task.title.lower()}
    for section in context.sections:
        if section.name != "known_failures":
            selected.append(section)
            continue
        content_lower = section.content.lower()
        if any(term and term in content_lower for term in failure_terms):
            selected.append(section)

    return ContextBundle(sections=selected)
