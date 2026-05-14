from __future__ import annotations

from pathlib import Path

from agentic_devloop.models import (
    ContextBundle,
    ContextBundleManifest,
    ContextPhase,
    ContextSection,
    ContextTruncationRecord,
    ProjectConfig,
    TaskContract,
)


STATE_FILES = [
    ("architecture_summary", "architecture_summary.md"),
    ("active_constraints", "active_constraints.yaml"),
    ("benchmark_status", "benchmark_status.json"),
    ("known_failures", "known_failures.md"),
    ("release_plan", "release_plan.yaml"),
    ("backlog_state", "backlog_state.yaml"),
]


class ContextBudgetError(ValueError):
    pass


def load_context_bundle(config: ProjectConfig, task: TaskContract) -> ContextBundle:
    if config.repo_state_path is None:
        return ContextBundle()

    return build_phase_context_bundle(config, task, phase=ContextPhase.WORKER)


def build_phase_context_bundle(
    config: ProjectConfig,
    task: TaskContract,
    *,
    phase: ContextPhase,
    max_chars: int | None = None,
) -> ContextBundle:
    if config.repo_state_path is None:
        return ContextBundle(
            sections=[],
            manifest=ContextBundleManifest(
                phase=phase,
                included_categories=[],
                omitted_categories=[],
                chars_by_category={},
                total_chars=0,
                truncation_records=[],
            ),
        )

    raw_sections = _load_repo_state_sections(config)
    requested_categories = _phase_categories(phase)
    selected_sections = _select_sections_for_phase(raw_sections, task, requested_categories)
    pruned = _truncate_sections(selected_sections, max_chars=max_chars)

    included = [section.name for section in pruned.sections]
    available = [section.name for section in raw_sections]
    omitted = [name for name in available if name not in included]
    chars_by_category = {section.name: len(section.content) for section in pruned.sections}

    return ContextBundle(
        sections=pruned.sections,
        manifest=ContextBundleManifest(
            phase=phase,
            included_categories=included,
            omitted_categories=omitted,
            chars_by_category=chars_by_category,
            total_chars=sum(chars_by_category.values()),
            truncation_records=pruned.truncation_records,
        ),
    )


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


def _load_repo_state_sections(config: ProjectConfig) -> list[ContextSection]:
    root = _resolve_state_path(config)
    sections: list[ContextSection] = []
    for name, filename in STATE_FILES:
        path = root / filename
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            continue
        sections.append(ContextSection(name=name, source_path=path, content=content))
    return sections


def _phase_categories(phase: ContextPhase) -> tuple[str, ...]:
    if phase == ContextPhase.WORKER:
        return tuple(name for name, _ in STATE_FILES)
    if phase == ContextPhase.REVIEW:
        return ("active_constraints", "release_plan", "backlog_state", "known_failures")
    if phase == ContextPhase.REPAIR:
        return ("active_constraints", "known_failures", "backlog_state")
    raise ValueError(f"unsupported context phase: {phase}")


def _select_sections_for_phase(
    sections: list[ContextSection], task: TaskContract, requested_categories: tuple[str, ...]
) -> list[ContextSection]:
    index = {section.name: section for section in sections}
    selected: list[ContextSection] = []
    for category in requested_categories:
        section = index.get(category)
        if section is None:
            continue
        if section.name != "known_failures":
            selected.append(section)
            continue
        if _known_failures_is_relevant(section, task):
            selected.append(section)
    return selected


def _known_failures_is_relevant(section: ContextSection, task: TaskContract) -> bool:
    failure_terms = {task.task_id.lower(), task.release_id.lower(), task.title.lower()}
    content_lower = section.content.lower()
    return any(term and term in content_lower for term in failure_terms)


class _TruncatedSections:
    def __init__(
        self, sections: list[ContextSection], truncation_records: list[ContextTruncationRecord]
    ) -> None:
        self.sections = sections
        self.truncation_records = truncation_records


def _truncate_sections(sections: list[ContextSection], *, max_chars: int | None) -> _TruncatedSections:
    if max_chars is None:
        return _TruncatedSections(sections=sections, truncation_records=[])
    if max_chars < 0:
        raise ValueError("max_chars must be >= 0 when provided")

    used = 0
    included: list[ContextSection] = []
    records: list[ContextTruncationRecord] = []
    for index, section in enumerate(sections):
        section_len = len(section.content)
        remaining = max_chars - used
        if remaining <= 0:
            records.append(
                ContextTruncationRecord(
                    category=section.name,
                    source_path=section.source_path,
                    original_chars=section_len,
                    included_chars=0,
                    omitted_chars=section_len,
                    reason="omitted_after_budget",
                )
            )
            continue

        if section_len <= remaining:
            included.append(section)
            used += section_len
            continue

        truncated_content = section.content[:remaining]
        included.append(
            ContextSection(name=section.name, source_path=section.source_path, content=truncated_content)
        )
        records.append(
            ContextTruncationRecord(
                category=section.name,
                source_path=section.source_path,
                original_chars=section_len,
                included_chars=len(truncated_content),
                omitted_chars=section_len - len(truncated_content),
                reason="truncated_to_budget",
            )
        )
        for remaining_section in sections[index + 1 :]:
            remaining_len = len(remaining_section.content)
            records.append(
                ContextTruncationRecord(
                    category=remaining_section.name,
                    source_path=remaining_section.source_path,
                    original_chars=remaining_len,
                    included_chars=0,
                    omitted_chars=remaining_len,
                    reason="omitted_after_budget",
                )
            )
        break

    return _TruncatedSections(sections=included, truncation_records=records)


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
