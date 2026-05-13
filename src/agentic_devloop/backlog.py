from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from pydantic import ValidationError

from agentic_devloop.budget import reserve_strong_model_call
from agentic_devloop.config import load_project_config
from agentic_devloop.models import (
    BacklogEpic,
    BacklogEvidenceManifest,
    BacklogPlan,
    GovernorCycleContinuation,
    GovernorStopReason,
    ReleaseObjective,
)
from agentic_devloop.objective import run_objective
from agentic_devloop.orchestrator import ExecutorProtocol
from agentic_devloop.planning import PlannerBackend
from agentic_devloop.process import run_process
from agentic_devloop.release import ReleaseRunResult
from agentic_devloop.yaml_io import write_yaml_model


@dataclass(frozen=True)
class BacklogPlanResult:
    plan_path: Path
    plan: BacklogPlan
    objective_path: Path | None = None


@dataclass(frozen=True)
class BacklogRunResult:
    selected_epic_id: str
    plan_path: Path
    backlog_plan_path: Path
    plan: BacklogPlan
    objective_path: Path
    objective: ReleaseObjective
    release_id: str
    release: ReleaseRunResult | None
    generated_objective_path: Path | None = None
    contract_plan_path: Path | None = None
    execution_strategy_selection_path: Path | None = None
    supervisor_decision_path: Path | None = None
    one_shot_execution_input_path: Path | None = None
    release_summary_path: Path | None = None
    release_metrics_path: Path | None = None
    release_budget_path: Path | None = None
    release_tuning_path: Path | None = None
    state_refresh_summary_path: Path | None = None
    finalization_policy: str | None = None
    finalization_result: dict[str, Any] | None = None
    cleanup_result: dict[str, Any] | None = None
    blocked_finalization: dict[str, Any] | None = None
    governor_cycle_continuation: GovernorCycleContinuation | None = None
    evidence_manifest: BacklogEvidenceManifest | None = None


@dataclass(frozen=True)
class BacklogMultiRunResult:
    project_id: str
    requested_epic_count: int
    attempted_epic_count: int
    accepted_epic_count: int
    cycles: list[BacklogRunResult]
    stop_reason: GovernorStopReason | str


@dataclass(frozen=True)
class BacklogPlannerBackendResult:
    raw_output: str | dict[str, Any] | BacklogPlan
    stdout_path: Path
    stderr_path: Path
    metadata_path: Path


class BacklogPlannerBackend(Protocol):
    def generate(
        self,
        *,
        prompt: str,
        goal: str,
        roadmap_text: str,
        model: str,
    ) -> str | dict[str, Any] | BacklogPlan | BacklogPlannerBackendResult:
        ...


class CodexBacklogPlannerBackend:
    def __init__(self, *, config, repo_path: Path, output_dir: Path | None = None) -> None:
        self.config = config
        self.repo_path = repo_path
        self.output_dir = output_dir or Path("runs") / "backlog_planner_backend"

    def with_output_dir(self, output_dir: Path) -> "CodexBacklogPlannerBackend":
        return CodexBacklogPlannerBackend(
            config=self.config,
            repo_path=self.repo_path,
            output_dir=output_dir,
        )

    def generate(
        self,
        *,
        prompt: str,
        goal: str,
        roadmap_text: str,
        model: str,
    ) -> BacklogPlannerBackendResult:
        del goal, roadmap_text
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = self.output_dir / "backlog_planner_stdout.log"
        stderr_path = self.output_dir / "backlog_planner_stderr.log"
        metadata_path = self.output_dir / "backlog_planner_metadata.json"
        command = [
            "codex",
            "exec",
            "--model",
            model,
            "--sandbox",
            "workspace-write",
            "-",
        ]
        result = run_process(
            command,
            cwd=self.repo_path,
            timeout_seconds=self.config.max_walltime_minutes * 60,
            input_text=prompt,
        )
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        metadata_path.write_text(
            json.dumps(
                {
                    "backend": self.config.type,
                    "model": model,
                    "command": [shlex.quote(part) for part in command],
                    "exit_code": result.exit_code,
                    "duration_seconds": result.duration_seconds,
                    "timed_out": result.timed_out,
                    "prompt_chars": len(prompt),
                    "stdout_chars": len(result.stdout),
                    "stderr_chars": len(result.stderr),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if result.exit_code != 0:
            quoted = " ".join(shlex.quote(part) for part in command)
            message = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"backlog planner command failed ({quoted}): {message}")
        return BacklogPlannerBackendResult(
            raw_output=result.stdout,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            metadata_path=metadata_path,
        )


@dataclass(frozen=True)
class _Candidate:
    text: str
    source_ref: str
    order: int


def make_backlog_plan_id(project_id: str, now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{project_id}_backlog"


def plan_backlog(
    *,
    project_id: str,
    goal: str,
    roadmap_path: Path = Path("docs/design/ROADMAP_AND_BACKLOG.md"),
    config_dir: Path = Path("configs"),
    runs_dir: Path = Path("runs"),
    objectives_dir: Path = Path("objectives"),
    write_objective: bool = False,
    mode: str = "deterministic",
    planner_backend: BacklogPlannerBackend | None = None,
    state_review_snapshot_path: Path | None = None,
    state_refresh_summary_path: Path | None = None,
    state_refresh_summary: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> BacklogPlanResult:
    config = load_project_config(project_id, config_dir, validate_repo=True)
    if mode not in {"deterministic", "strong-model"}:
        raise ValueError(f"unsupported backlog planning mode: {mode}")
    roadmap_text = roadmap_path.read_text(encoding="utf-8")
    warnings: list[str] = []
    plan_dir = runs_dir / make_backlog_plan_id(config.project_id, now)
    plan_dir.mkdir(parents=True, exist_ok=True)
    planner_prompt_path = None
    backend_paths: dict[str, Path] = {}
    plan: BacklogPlan | None = None

    if mode == "strong-model":
        planner = config.model_roles.get("planner", config.executor)
        reserve_strong_model_call(
            runs_dir=runs_dir,
            release_id=f"{config.project_id}-backlog",
            budget=config.budget,
            model=planner.model,
            reason="backlog planning",
            now=now,
        )
        planner_prompt = _backlog_planner_prompt(
            project_id=config.project_id,
            goal=goal,
            roadmap_path=roadmap_path,
            roadmap_text=roadmap_text,
            documentation=_documentation_context(config.repo_path, roadmap_path),
            repo_state=_repo_state_context(config.repo_state_path),
            state_review_snapshot_path=state_review_snapshot_path,
            state_refresh_summary_path=state_refresh_summary_path,
            state_refresh_summary=state_refresh_summary,
        )
        planner_prompt_path = plan_dir / "backlog_planner_prompt.md"
        planner_prompt_path.write_text(planner_prompt, encoding="utf-8")
        if planner_backend is None:
            warnings.append(
                "Strong-model backlog planning backend was not executed; planner prompt was written."
            )
        else:
            backend = _backlog_backend_for_plan(
                planner_backend,
                plan_dir / "backlog_planner_backend",
            )
            backend_output = backend.generate(
                prompt=planner_prompt,
                goal=goal,
                roadmap_text=roadmap_text,
                model=planner.model,
            )
            backend_paths = _backlog_backend_paths(backend_output)
            raw_output = (
                backend_output.raw_output
                if isinstance(backend_output, BacklogPlannerBackendResult)
                else backend_output
            )
            plan = parse_backlog_planner_output(raw_output, project_id=config.project_id)
            plan = plan.model_copy(
                update={
                    "planner": mode,
                    "goal": goal,
                    "roadmap_path": roadmap_path,
                    "planner_prompt_path": planner_prompt_path,
                    "state_review_snapshot_path": state_review_snapshot_path,
                    "state_refresh_summary_path": state_refresh_summary_path,
                    **backend_paths,
                    "warnings": [*plan.warnings, *warnings],
                }
            )

    if plan is None:
        plan = _deterministic_backlog_plan(
            project_id=config.project_id,
            goal=goal,
            roadmap_path=roadmap_path,
            roadmap_text=roadmap_text,
            warnings=warnings,
            now=now,
        )
        plan = plan.model_copy(
            update={
                "state_review_snapshot_path": state_review_snapshot_path,
                "state_refresh_summary_path": state_refresh_summary_path,
            }
        )
        if planner_prompt_path is not None:
            plan = plan.model_copy(update={"planner_prompt_path": planner_prompt_path})

    objective_path = _write_selected_objective(plan, objectives_dir) if write_objective else None
    if objective_path is not None:
        plan = plan.model_copy(update={"objective_path": objective_path})

    plan_path = plan_dir / "backlog_plan.json"
    plan_path.write_text(json.dumps(plan.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return BacklogPlanResult(plan_path=plan_path, plan=plan, objective_path=objective_path)


def run_backlog(
    *,
    project_id: str,
    goal: str,
    roadmap_path: Path = Path("docs/design/ROADMAP_AND_BACKLOG.md"),
    selected_epic_id: str | None = None,
    config_dir: Path = Path("configs"),
    contracts_dir: Path = Path("contracts"),
    runs_dir: Path = Path("runs"),
    objectives_dir: Path = Path("objectives"),
    mode: str = "strong-model",
    planner_backend: BacklogPlannerBackend | None = None,
    objective_planner_backend: PlannerBackend | None = None,
    executor: ExecutorProtocol | None = None,
    verification_timeout_seconds: int = 600,
    allow_dirty: bool = False,
    commit_on_accept: bool = False,
    merge_on_accept: bool = False,
    push_on_accept: bool = False,
    release_finalize: str = "none",
    integration_branch: str | None = None,
    stop_on_failure: bool = True,
    execution_mode: str = "sequential",
    debug_keep_artifacts: bool = False,
    progress: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> BacklogRunResult:
    from agentic_devloop.governor import GovernorLoop

    return GovernorLoop(plan_backlog=plan_backlog, run_objective=run_objective).run_one_epic(
        project_id=project_id,
        goal=goal,
        roadmap_path=roadmap_path,
        selected_epic_id=selected_epic_id,
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=runs_dir,
        objectives_dir=objectives_dir,
        mode=mode,
        planner_backend=planner_backend,
        objective_planner_backend=objective_planner_backend,
        executor=executor,
        verification_timeout_seconds=verification_timeout_seconds,
        allow_dirty=allow_dirty,
        commit_on_accept=commit_on_accept,
        merge_on_accept=merge_on_accept,
        push_on_accept=push_on_accept,
        release_finalize=release_finalize,
        integration_branch=integration_branch,
        stop_on_failure=stop_on_failure,
        execution_mode=execution_mode,
        debug_keep_artifacts=debug_keep_artifacts,
        progress=progress,
        now=now,
    )


def run_governor(
    *,
    project_id: str,
    goal: str,
    epic_count: int,
    roadmap_path: Path = Path("docs/design/ROADMAP_AND_BACKLOG.md"),
    selected_epic_id: str | None = None,
    config_dir: Path = Path("configs"),
    contracts_dir: Path = Path("contracts"),
    runs_dir: Path = Path("runs"),
    objectives_dir: Path = Path("objectives"),
    mode: str = "strong-model",
    planner_backend: BacklogPlannerBackend | None = None,
    objective_planner_backend: PlannerBackend | None = None,
    executor: ExecutorProtocol | None = None,
    verification_timeout_seconds: int = 600,
    allow_dirty: bool = False,
    commit_on_accept: bool = False,
    merge_on_accept: bool = False,
    push_on_accept: bool = False,
    release_finalize: str = "none",
    integration_branch: str | None = None,
    stop_on_failure: bool = True,
    execution_mode: str = "sequential",
    debug_keep_artifacts: bool = False,
    progress: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> BacklogMultiRunResult:
    from agentic_devloop.governor import GovernorLoop
    from agentic_devloop.state_store import StateStore

    config = load_project_config(project_id, config_dir, validate_repo=False)
    repo_state_root = config.repo_state_path or Path("repo_state") / project_id
    state_store = StateStore(repo_state_root / "backlog_state.yaml")
    return GovernorLoop(
        plan_backlog=plan_backlog,
        run_objective=run_objective,
        state_store=state_store,
    ).run_epics(
        project_id=project_id,
        goal=goal,
        roadmap_path=roadmap_path,
        selected_epic_id=selected_epic_id,
        epic_count=epic_count,
        config_dir=config_dir,
        contracts_dir=contracts_dir,
        runs_dir=runs_dir,
        objectives_dir=objectives_dir,
        mode=mode,
        planner_backend=planner_backend,
        objective_planner_backend=objective_planner_backend,
        executor=executor,
        verification_timeout_seconds=verification_timeout_seconds,
        allow_dirty=allow_dirty,
        commit_on_accept=commit_on_accept,
        merge_on_accept=merge_on_accept,
        push_on_accept=push_on_accept,
        release_finalize=release_finalize,
        integration_branch=integration_branch,
        stop_on_failure=stop_on_failure,
        execution_mode=execution_mode,
        debug_keep_artifacts=debug_keep_artifacts,
        progress=progress,
        now=now,
    )


def parse_backlog_planner_output(
    raw_output: str | dict[str, Any] | BacklogPlan,
    *,
    project_id: str,
) -> BacklogPlan:
    if isinstance(raw_output, str):
        raw_output = _extract_json_object(raw_output)
        try:
            raw_output = json.loads(raw_output)
        except json.JSONDecodeError as error:
            raise ValueError("backlog planner output must be valid JSON") from error
    try:
        plan = BacklogPlan.model_validate(raw_output)
    except ValidationError as error:
        raise ValueError("backlog planner output did not match the BacklogPlan schema") from error
    if plan.project_id != project_id:
        raise ValueError(
            f"backlog planner output project_id {plan.project_id!r} did not match expected {project_id!r}"
        )
    selected_ids = {epic.epic_id for epic in plan.epics}
    if plan.selected_epic_id is not None and plan.selected_epic_id not in selected_ids:
        raise ValueError(f"selected_epic_id not found in epics: {plan.selected_epic_id}")
    return plan


def _deterministic_backlog_plan(
    *,
    project_id: str,
    goal: str,
    roadmap_path: Path,
    roadmap_text: str,
    warnings: list[str],
    now: datetime | None,
) -> BacklogPlan:
    candidates = _extract_candidates(roadmap_text)
    if not candidates:
        warnings.append("No actionable roadmap candidates found; generated a roadmap-review epic.")
        candidates = [
            _Candidate(
                text="Review roadmap, repo state, and recent run artifacts to identify the next bounded development epic.",
                source_ref=str(roadmap_path),
                order=1,
            )
        ]
    epics = _prioritized_epics(
        project_id=project_id,
        goal=goal,
        candidates=candidates,
        now=now,
    )
    selected = epics[0] if epics else None
    return BacklogPlan(
        project_id=project_id,
        goal=goal,
        roadmap_path=roadmap_path,
        planner="deterministic",
        epics=epics,
        selected_epic_id=selected.epic_id if selected else None,
        warnings=warnings,
    )


def _write_selected_objective(plan: BacklogPlan, objectives_dir: Path) -> Path | None:
    selected = next((epic for epic in plan.epics if epic.epic_id == plan.selected_epic_id), None)
    if selected is None:
        return None
    objectives_dir.mkdir(parents=True, exist_ok=True)
    objective = ReleaseObjective(
        release_id=selected.suggested_release_id,
        title=selected.title,
        objective=selected.objective,
        non_goals=[
            "Do not execute implementation workers while planning the backlog.",
            "Do not broaden the selected epic beyond its acceptance criteria.",
        ],
        acceptance_criteria=selected.acceptance_criteria,
    )
    return write_yaml_model(
        objectives_dir / f"{selected.suggested_release_id}.yaml",
        objective,
    )


def _backlog_planner_prompt(
    *,
    project_id: str,
    goal: str,
    roadmap_path: Path,
    roadmap_text: str,
    documentation: list[tuple[Path, str]],
    repo_state: list[tuple[Path, str]],
    state_review_snapshot_path: Path | None,
    state_refresh_summary_path: Path | None,
    state_refresh_summary: dict[str, Any] | None,
) -> str:
    doc_sections = []
    for path, content in documentation:
        doc_sections.extend([f"## Documentation: {path}", _truncate(content, 12000), ""])
    state_sections = []
    for path, content in repo_state:
        state_sections.extend([f"## Repo State: {path}", _truncate(content, 12000), ""])
    refresh_sections: list[str] = []
    if state_review_snapshot_path is not None:
        refresh_sections.extend(["## State Review Snapshot", f"path: {state_review_snapshot_path}", ""])
    if state_refresh_summary_path is not None or state_refresh_summary is not None:
        refresh_sections.append("## State Refresh Summary")
        if state_refresh_summary_path is not None:
            refresh_sections.append(f"path: {state_refresh_summary_path}")
        if state_refresh_summary is not None:
            concise_fields = (
                "branch",
                "head_commit",
                "status_count",
                "local_branch_count",
                "worktree_count",
                "repo_state_file_count",
                "recent_release_run_count",
            )
            for key in concise_fields:
                if key in state_refresh_summary:
                    refresh_sections.append(f"{key}: {state_refresh_summary[key]}")
        refresh_sections.append("")
    return "\n".join(
        [
            "# Autonomous Roadmap Governor",
            "",
            "You are the high-level development governor for this repository.",
            "Read the supplied documentation, roadmap, repository goal, and available state.",
            "Identify the next highest-priority, highest-reward development epics.",
            "Select exactly one next epic as selected_epic_id.",
            "The selected epic must be sized so a follow-up planner can decompose it into bounded task contracts.",
            "Prefer autonomous execution paths. Do not insert human approval gates unless the repository policy or safety constraints require them.",
            "Continuously update the roadmap and backlog from new evidence. Put proposed roadmap changes in roadmap_updates and repo-state memory changes in repo_state_updates.",
            "For simulation or validation-heavy repositories, treat new domain findings, benchmark learnings, failed validations, and artifact evidence as backlog inputs.",
            "Do not choose cleanup work unless it directly improves autonomous roadmap-driven development.",
            "Return only one JSON object matching the BacklogPlan schema.",
            "",
            "Required JSON shape:",
            '{"project_id": "...", "goal": "...", "roadmap_path": "...", "planner": "strong-model", "epics": [{"epic_id": "epic-0001", "title": "...", "objective": "...", "rationale": "...", "priority": 1, "source_refs": ["..."], "acceptance_criteria": ["..."], "suggested_release_id": "..."}], "selected_epic_id": "epic-0001", "warnings": [], "roadmap_updates": ["..."], "repo_state_updates": ["..."]}',
            "",
            f"Project ID: {project_id}",
            f"Repository goal: {goal}",
            f"Roadmap path: {roadmap_path}",
            "",
            *doc_sections,
            *state_sections,
            *refresh_sections,
            "## Roadmap",
            _truncate(roadmap_text, 30000),
        ]
    )


def _documentation_context(repo_path: Path, roadmap_path: Path) -> list[tuple[Path, str]]:
    candidates = [
        repo_path / "AGENTS.md",
        repo_path / "README.md",
        repo_path / "docs" / "README.md",
        repo_path / "docs" / "design" / "ARCHITECTURE.md",
        repo_path / "docs" / "design" / "TECHNICAL_SPECIFICATION.md",
    ]
    roadmap_resolved = roadmap_path if roadmap_path.is_absolute() else Path.cwd() / roadmap_path
    docs: list[tuple[Path, str]] = []
    for path in candidates:
        if path.resolve() == roadmap_resolved.resolve():
            continue
        if path.exists():
            docs.append((path, path.read_text(encoding="utf-8")))
    return docs


def _repo_state_context(repo_state_path: Path | None) -> list[tuple[Path, str]]:
    if repo_state_path is None or not repo_state_path.exists():
        return []
    files = [
        "architecture_summary.md",
        "active_constraints.yaml",
        "known_failures.md",
        "benchmark_status.json",
        "release_plan.yaml",
        "backlog_state.yaml",
    ]
    state: list[tuple[Path, str]] = []
    for name in files:
        path = repo_state_path / name
        if path.exists():
            state.append((path, path.read_text(encoding="utf-8")))
    return state


def _backlog_backend_for_plan(
    planner_backend: BacklogPlannerBackend,
    output_dir: Path,
) -> BacklogPlannerBackend:
    with_output_dir = getattr(planner_backend, "with_output_dir", None)
    if callable(with_output_dir):
        return with_output_dir(output_dir)
    return planner_backend


def _backlog_backend_paths(raw_output: object) -> dict[str, Path]:
    if not isinstance(raw_output, BacklogPlannerBackendResult):
        return {}
    return {
        "planner_stdout_path": raw_output.stdout_path,
        "planner_stderr_path": raw_output.stderr_path,
        "planner_metadata_path": raw_output.metadata_path,
    }


def _extract_json_object(raw_output: str) -> str:
    stripped = raw_output.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    if fenced:
        return fenced.group(1)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        return stripped
    return stripped[start : end + 1]


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[truncated]\n"


def _extract_candidates(roadmap_text: str) -> list[_Candidate]:
    remaining_candidates: list[_Candidate] = []
    loose_candidates: list[_Candidate] = []
    in_remaining_section = False
    remaining_seen_item = False
    for line_number, raw_line in enumerate(roadmap_text.splitlines(), start=1):
        stripped = raw_line.strip()
        lower = stripped.lower()
        if lower.startswith("remaining ") or lower.startswith("remaining phase"):
            in_remaining_section = True
            remaining_seen_item = False
            continue
        if in_remaining_section and stripped.startswith("## "):
            in_remaining_section = False
        if in_remaining_section and not stripped and remaining_seen_item:
            in_remaining_section = False

        numbered = re.match(r"^\d+\.\s+(.+)$", stripped)
        bullet = re.match(r"^-\s+(.+)$", stripped)
        text = numbered.group(1) if numbered else bullet.group(1) if bullet else ""
        if not text:
            continue
        actionable = in_remaining_section or any(
            marker in lower
            for marker in (
                "not implemented",
                "should be",
                "should ",
                "future",
                "remaining",
                "generalized",
                "not yet",
                "cleanup",
                "automation",
            )
        )
        if actionable:
            target = remaining_candidates if in_remaining_section else loose_candidates
            target.append(
                _Candidate(
                    text=text.rstrip("."),
                    source_ref=f"roadmap:{line_number}",
                    order=len(target) + 1,
                )
            )
            if in_remaining_section:
                remaining_seen_item = True
    return _dedupe_candidates(remaining_candidates or loose_candidates)


def _dedupe_candidates(candidates: list[_Candidate]) -> list[_Candidate]:
    seen: set[str] = set()
    deduped: list[_Candidate] = []
    for candidate in candidates:
        key = _slug(candidate.text)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _prioritized_epics(
    *,
    project_id: str,
    goal: str,
    candidates: list[_Candidate],
    now: datetime | None,
) -> list[BacklogEpic]:
    goal_terms = _terms(goal)
    scored = []
    for candidate in candidates:
        score = _score_candidate(candidate, goal_terms)
        scored.append((score, candidate.order, candidate))
    scored.sort(key=lambda item: (-item[0], item[1]))
    epics: list[BacklogEpic] = []
    for priority, (_, _, candidate) in enumerate(scored, start=1):
        release_id = _release_id(project_id, candidate, now)
        epics.append(
            BacklogEpic(
                epic_id=f"epic-{priority:04d}",
                title=_title(candidate.text),
                objective=candidate.text,
                rationale=_rationale(candidate.text, goal),
                priority=priority,
                source_refs=[candidate.source_ref],
                acceptance_criteria=_acceptance_criteria(candidate.text),
                suggested_release_id=release_id,
            )
        )
    return epics


def _score_candidate(candidate: _Candidate, goal_terms: set[str]) -> int:
    candidate_terms = _terms(candidate.text)
    score = 10 * len(candidate_terms & goal_terms)
    text = candidate.text.lower()
    if "repository instruction" in text or "roadmap" in text:
        score += 8
    if "run-backlog" in text:
        score += 24
    if "governor" in text or "persistent backlog" in text:
        score += 16
    if "objective" in text or "contract" in text or "pr" in text:
        score += 5
    if "rename" in text or "cleanup" in text or "alias" in text:
        score += 2
    return score


def _terms(text: str) -> set[str]:
    return {
        term
        for term in re.findall(r"[a-z0-9]+", text.lower())
        if len(term) > 3 and term not in {"that", "with", "from", "this", "into", "when"}
    }


def _title(text: str) -> str:
    words = text.split()
    title = " ".join(words[:10]).strip()
    return title[0].upper() + title[1:] if title else "Backlog Epic"


def _rationale(text: str, goal: str) -> str:
    return (
        "Selected from roadmap analysis because it advances the repository goal: "
        f"{goal}. Roadmap item: {text}."
    )


def _acceptance_criteria(text: str) -> list[str]:
    return [
        "A bounded release objective exists for the selected epic.",
        "Task contracts can be generated from the objective without whole-repo scope.",
        f"The implemented increment addresses: {text}.",
        "Relevant tests and documentation are updated.",
    ]


def _release_id(project_id: str, candidate: _Candidate, now: datetime | None) -> str:
    date = (now or datetime.now(UTC)).strftime("%Y%m%d")
    slug = _slug(candidate.text)[:36].strip("-") or "backlog-epic"
    return f"{project_id}-{date}-{slug}"


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return re.sub(r"-+", "-", slug)
