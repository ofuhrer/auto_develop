from __future__ import annotations

import json
import shlex
import shutil
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from agentic_devloop.config import load_project_config
from agentic_devloop.git_state import git_text
from agentic_devloop.models import (
    ExecutorConfig,
    FeatureReviewDecision,
    FeatureReviewFinding,
    FeatureReviewRecommendation,
    FeatureReviewSeverity,
    GeneratedContract,
    Reviewer,
    TaskContract,
)
from agentic_devloop.process import run_process


class FeatureReviewContextError(ValueError):
    pass


class FeatureReviewClassificationError(ValueError):
    pass


@dataclass(frozen=True)
class FeatureReviewBranches:
    base_branch: str
    integration_branch: str


@dataclass(frozen=True)
class FeatureReviewContext:
    release_id: str
    base_branch: str
    integration_branch: str
    base_commit: str
    integration_commit: str
    changed_files: list[str]
    diff_text: str
    docs_design_paths: list[Path]
    latest_release_run_dir: Path | None
    release_summary_path: Path | None
    release_review_path: Path | None
    release_metrics_path: Path | None
    release_budget_path: Path | None
    release_tuning_path: Path | None
    release_objective: str | None = None
    diff_stat_text: str = ""
    diff_numstat_text: str = ""
    changed_file_excerpts: list[tuple[str, str]] = field(default_factory=list)
    accepted_repair_history: list[str] = field(default_factory=list)
    prior_feature_review_path: Path | None = None
    prior_feature_review_recheck_path: Path | None = None
    final_integration_verification_path: Path | None = None
    final_integration_verification_log_path: Path | None = None
    final_integration_worktree_log_path: Path | None = None


@dataclass(frozen=True)
class FeatureReviewBackendResult:
    decision: FeatureReviewDecision
    prompt_path: Path
    stdout_path: Path
    stderr_path: Path
    metadata_path: Path
    raw_output: str


FeatureReviewFindingClassification = Literal[
    "blocker",
    "soft_finding",
    "duplicate",
    "false_positive",
    "scope_expansion",
    "backlog_follow_up",
]
FeatureReviewFindingAction = Literal["repair", "accept", "defer"]


@dataclass(frozen=True)
class FeatureReviewFindingConvergenceResult:
    finding_id: str
    classification: FeatureReviewFindingClassification
    selected_action: FeatureReviewFindingAction
    matched_previous_finding_id: str | None
    repeated_by_finding_id: bool
    adjacent_similarity: float
    verification_false_positive_candidate: bool


@dataclass(frozen=True)
class FeatureReviewConvergenceResult:
    findings: list[FeatureReviewFindingConvergenceResult]
    blocking_finding_ids: list[str]
    accepted_finding_ids: list[str]
    deferred_finding_ids: list[str]
    false_positive_candidate_ids: list[str]


UNSAFE_REPAIR_FILENAMES: frozenset[str] = frozenset(
    {
        "poetry.lock",
        "uv.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
    }
)

UNSAFE_REPAIR_DIRNAMES: frozenset[str] = frozenset({"migrations", "generated"})

MAX_FEATURE_REVIEW_DIFF_CHARS = 120_000
MAX_FEATURE_REVIEW_ARTIFACT_CHARS = 40_000
MAX_FEATURE_REVIEW_DIFF_SUMMARY_CHARS = 12_000
MAX_FEATURE_REVIEW_CHANGED_FILE_EXCERPTS = 8
MAX_FEATURE_REVIEW_CHANGED_FILE_EXCERPT_CHARS = 3_000


def determine_feature_review_branches(
    *,
    release_id: str,
    base_branch: str,
    integration_branch: str | None,
) -> FeatureReviewBranches:
    feature_branch = integration_branch or f"feature/{release_id}"
    if not base_branch.strip():
        raise FeatureReviewContextError("base branch must not be empty")
    if not feature_branch.strip():
        raise FeatureReviewContextError("integration branch must not be empty")
    return FeatureReviewBranches(base_branch=base_branch, integration_branch=feature_branch)


def load_feature_review_branches(
    *,
    project_id: str,
    release_id: str,
    config_dir: Path = Path("configs"),
    integration_branch: str | None = None,
) -> FeatureReviewBranches:
    config = load_project_config(project_id, config_dir, validate_repo=False)
    return determine_feature_review_branches(
        release_id=release_id,
        base_branch=config.default_base_branch,
        integration_branch=integration_branch,
    )


def assemble_feature_review_context(
    *,
    repo_path: Path,
    release_id: str,
    base_branch: str,
    integration_branch: str,
    runs_dir: Path = Path("runs"),
    docs_design_dir: Path = Path("docs/design"),
    release_objective: str | None = None,
) -> FeatureReviewContext:
    repo_path = repo_path.resolve()
    runs_root = (repo_path / runs_dir).resolve()
    docs_design_root = (repo_path / docs_design_dir).resolve()

    _ensure_git_ref(repo_path, base_branch)
    _ensure_git_ref(repo_path, integration_branch)

    base_commit = _git_rev(repo_path, base_branch)
    integration_commit = _git_rev(repo_path, integration_branch)
    diff_text = git_text(repo_path, ["diff", "--patch", f"{base_branch}..{integration_branch}"])
    diff_stat_text = git_text(repo_path, ["diff", "--stat", f"{base_branch}..{integration_branch}"])
    diff_numstat_text = git_text(repo_path, ["diff", "--numstat", f"{base_branch}..{integration_branch}"])
    changed_files = [
        line
        for line in git_text(repo_path, ["diff", "--name-only", f"{base_branch}..{integration_branch}"])
        .splitlines()
        if line.strip()
    ]
    changed_file_excerpts = _collect_changed_file_excerpts(
        repo_path=repo_path,
        integration_branch=integration_branch,
        changed_files=changed_files,
    )

    docs_design_paths = sorted(_safe_glob_files(docs_design_root, pattern="**/*"))

    latest_release_run_dir, summary_path = _latest_release_summary(
        runs_root=runs_root,
        release_id=release_id,
        integration_branch=integration_branch,
        integration_commit=integration_commit,
    )
    if latest_release_run_dir is None:
        release_review_path = None
        metrics_path = None
        budget_path = None
        tuning_path = None
        accepted_repair_history: list[str] = []
        prior_feature_review_path = None
        prior_feature_review_recheck_path = None
        final_integration_verification_path = None
        final_integration_verification_log_path = None
        final_integration_worktree_log_path = None
    else:
        release_review_path = _safe_optional(latest_release_run_dir / "release_review.md", runs_root)
        metrics_path = _safe_optional(latest_release_run_dir / "release_metrics.json", runs_root)
        budget_path = _safe_optional(latest_release_run_dir / "release_budget.json", runs_root)
        tuning_path = _safe_optional(latest_release_run_dir / "release_tuning.md", runs_root)
        (
            accepted_repair_history,
            prior_feature_review_path,
            prior_feature_review_recheck_path,
            final_integration_verification_path,
            final_integration_verification_log_path,
            final_integration_worktree_log_path,
        ) = _release_review_artifact_context(latest_release_run_dir=latest_release_run_dir, runs_root=runs_root)

    return FeatureReviewContext(
        release_id=release_id,
        base_branch=base_branch,
        integration_branch=integration_branch,
        base_commit=base_commit,
        integration_commit=integration_commit,
        changed_files=changed_files,
        diff_text=diff_text,
        docs_design_paths=docs_design_paths,
        latest_release_run_dir=latest_release_run_dir,
        release_summary_path=summary_path,
        release_review_path=release_review_path,
        release_metrics_path=metrics_path,
        release_budget_path=budget_path,
        release_tuning_path=tuning_path,
        release_objective=release_objective.strip() if isinstance(release_objective, str) and release_objective.strip() else None,
        diff_stat_text=diff_stat_text,
        diff_numstat_text=diff_numstat_text,
        changed_file_excerpts=changed_file_excerpts,
        accepted_repair_history=accepted_repair_history,
        prior_feature_review_path=prior_feature_review_path,
        prior_feature_review_recheck_path=prior_feature_review_recheck_path,
        final_integration_verification_path=final_integration_verification_path,
        final_integration_verification_log_path=final_integration_verification_log_path,
        final_integration_worktree_log_path=final_integration_worktree_log_path,
    )


def render_feature_review_prompt(
    *,
    context: FeatureReviewContext,
    repo_path: Path,
    runs_dir: Path = Path("runs"),
    docs_design_dir: Path = Path("docs/design"),
) -> str:
    repo_root = repo_path.resolve()
    runs_root = (repo_root / runs_dir).resolve()
    docs_design_root = (repo_root / docs_design_dir).resolve()

    def read(path: Path | None) -> str:
        if path is None:
            return ""
        text = _safe_read_text(path, allowed_roots=[repo_root, runs_root, docs_design_root])
        return _bounded_review_text(
            text,
            max_chars=MAX_FEATURE_REVIEW_ARTIFACT_CHARS,
            evidence_path=_repo_relative(path, repo_root),
            label=path.name,
        )

    docs_section = _render_docs_design_section(
        context=context,
        repo_root=repo_root,
        runs_root=runs_root,
        docs_design_root=docs_design_root,
    )
    diff_text = _bounded_review_text(
        context.diff_text,
        max_chars=MAX_FEATURE_REVIEW_DIFF_CHARS,
        evidence_path=f"git diff --patch {context.base_branch}..{context.integration_branch}",
        label="git diff",
    )
    release_summary = read(context.release_summary_path)
    release_review = read(context.release_review_path)
    release_metrics = read(context.release_metrics_path)
    release_budget = read(context.release_budget_path)
    release_tuning = read(context.release_tuning_path)
    diff_stat = _bounded_review_text(
        context.diff_stat_text,
        max_chars=MAX_FEATURE_REVIEW_DIFF_SUMMARY_CHARS,
        evidence_path=f"git diff --stat {context.base_branch}..{context.integration_branch}",
        label="git diff --stat",
    )
    diff_numstat = _bounded_review_text(
        context.diff_numstat_text,
        max_chars=MAX_FEATURE_REVIEW_DIFF_SUMMARY_CHARS,
        evidence_path=f"git diff --numstat {context.base_branch}..{context.integration_branch}",
        label="git diff --numstat",
    )

    schema = {
        "release_id": "<str>",
        "reviewer": "<human|strong_model|deterministic|hybrid>",
        "summary": "<str>",
        "recommendation": "<approve|approve_with_repairs|require_repairs|escalate>",
        "accepted_risks": ["<str>", "..."],
        "rerun_verification_commands": ["<str>", "..."],
        "findings": [
            {
                "finding_id": "<str>",
                "severity": "<low|moderate|high|critical>",
                "summary": "<str>",
                "affected_files": ["<str>", "..."],
                "evidence_paths": ["<str>", "..."],
                "required_repairs": ["<str>", "..."],
                "optional_follow_ups": ["<str>", "..."],
            }
        ],
    }

    return "\n".join(
        [
            "You are an independent reviewer agent for a Git feature branch integration.",
            "Return ONLY a single JSON object matching the schema below (no markdown fences, no prose).",
            "",
            "Schema (JSON):",
            json.dumps(schema, indent=2),
            "",
            f"Release: {context.release_id}",
            f"Base branch: {context.base_branch} @ {context.base_commit}",
            f"Integration branch: {context.integration_branch} @ {context.integration_commit}",
            f"Release objective: {context.release_objective or '(not provided)'}",
            "",
            "Changed files (base..integration):",
            *([f"- {path}" for path in context.changed_files] or ["- (none)"]),
            "",
            "Integration diff summary:",
            f"git diff --stat:\n{diff_stat}".rstrip(),
            "",
            f"git diff --numstat:\n{diff_numstat}".rstrip(),
            "",
            "Git diff (base..integration):",
            diff_text,
            "",
            "Latest release artifacts (if present):",
            f"release_summary.json:\n{release_summary}".rstrip(),
            "",
            f"release_review.md:\n{release_review}".rstrip(),
            "",
            f"release_metrics.json:\n{release_metrics}".rstrip(),
            "",
            f"release_budget.json:\n{release_budget}".rstrip(),
            "",
            f"release_tuning.md:\n{release_tuning}".rstrip(),
            "",
            "Prior review/recheck artifacts (latest matching release run):",
            f"- feature_review_path: {context.prior_feature_review_path or '(missing)'}",
            f"- feature_review_recheck_path: {context.prior_feature_review_recheck_path or '(missing)'}",
            f"- final_integration_verification_path: {context.final_integration_verification_path or '(missing)'}",
            f"- final_integration_verification_log_path: {context.final_integration_verification_log_path or '(missing)'}",
            f"- final_integration_worktree_log_path: {context.final_integration_worktree_log_path or '(missing)'}",
            "",
            "Accepted repair history (latest matching release run):",
            *([f"- {line}" for line in context.accepted_repair_history] or ["- (none)"]),
            "",
            "Relevant changed-file excerpts (bounded):",
            *(
                ["- (none)"]
                if not context.changed_file_excerpts
                else [
                    block
                    for path, excerpt in context.changed_file_excerpts
                    for block in (
                        (
                            f"{path}:\n"
                            + _bounded_review_text(
                                excerpt,
                                max_chars=MAX_FEATURE_REVIEW_CHANGED_FILE_EXCERPT_CHARS,
                                evidence_path=f"git show {context.integration_branch}:{path}",
                                label=f"excerpt {path}",
                            )
                        ).rstrip(),
                        "",
                    )
                ]
            ),
            "",
            docs_section,
            "",
            "Instructions:",
            "- Prefer recommendation approve/approve_with_repairs/require_repairs based on findings.",
            "- Use recommendation escalate when review is blocked (missing context, backend/tooling issues, unclear base/integration, etc.).",
            "- Findings should be actionable and refer to evidence paths when relevant (e.g., runs artifacts or changed files).",
            "- Keep summary concise and ensure all list items are non-empty strings.",
        ]
    ).strip() + "\n"


def invoke_feature_reviewer(
    *,
    config: ExecutorConfig,
    repo_path: Path,
    prompt: str,
    release_id: str,
    output_dir: Path,
    model: str | None = None,
) -> FeatureReviewBackendResult:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = output_dir / "feature_review_prompt.md"
    stdout_path = output_dir / "feature_review_stdout.log"
    stderr_path = output_dir / "feature_review_stderr.log"
    metadata_path = output_dir / "feature_review_metadata.json"

    prompt_path.write_text(prompt, encoding="utf-8")

    preflight_error = _feature_review_backend_preflight_error(config=config)
    if preflight_error is not None:
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(preflight_error + "\n", encoding="utf-8")
        metadata_path.write_text(
            json.dumps(
                {
                    "configured_backend": config.type,
                    "backend": None,
                    "model": model or config.model,
                    "command": [],
                    "exit_code": None,
                    "duration_seconds": 0.0,
                    "timed_out": False,
                    "prompt_chars": len(prompt),
                    "stdout_chars": 0,
                    "stderr_chars": len(preflight_error) + 1,
                    "error": preflight_error,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        decision = _blocked_feature_review_decision(
            release_id=release_id,
            reason=preflight_error,
            evidence_paths=[prompt_path, stdout_path, stderr_path, metadata_path],
        )
        return FeatureReviewBackendResult(
            decision=decision,
            prompt_path=prompt_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            metadata_path=metadata_path,
            raw_output="",
        )

    command = [
        "codex",
        "exec",
        "--model",
        model or config.model,
        "--sandbox",
        "workspace-write",
        "-",
    ]
    result = run_process(
        command,
        cwd=repo_path,
        timeout_seconds=config.max_walltime_minutes * 60,
        input_text=prompt,
    )
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    metadata_path.write_text(
        json.dumps(
            {
                "configured_backend": config.type,
                "backend": "codex_cli",
                "model": model or config.model,
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
        decision = _blocked_feature_review_decision(
            release_id=release_id,
            reason=_process_error_message(result.stdout, result.stderr),
            evidence_paths=[prompt_path, stdout_path, stderr_path, metadata_path],
        )
        return FeatureReviewBackendResult(
            decision=decision,
            prompt_path=prompt_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            metadata_path=metadata_path,
            raw_output=result.stdout,
        )

    try:
        payload = _parse_json_object(result.stdout)
        decision = FeatureReviewDecision.model_validate(payload)
    except Exception as error:  # noqa: BLE001 - evidence is recorded in structured findings
        decision = _blocked_feature_review_decision(
            release_id=release_id,
            reason=f"Reviewer output was not valid FeatureReviewDecision JSON: {error}",
            evidence_paths=[prompt_path, stdout_path, stderr_path, metadata_path],
        )

    return FeatureReviewBackendResult(
        decision=decision,
        prompt_path=prompt_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        metadata_path=metadata_path,
        raw_output=result.stdout,
    )


def _feature_review_backend_preflight_error(*, config: ExecutorConfig) -> str | None:
    if config.type != "codex_cli":
        return (
            f"Unsupported feature review executor backend: {config.type}. "
            "Only codex_cli is supported for feature review."
        )
    if shutil.which("codex") is None:
        return (
            "Feature review backend is unavailable: `codex` was not found on PATH. "
            "Install the Codex CLI and ensure `codex --version` succeeds, or unset "
            "`model_roles.reviewer` to disable semantic feature review."
        )
    return None


def generate_repair_contracts_for_required_findings(
    *,
    decision: FeatureReviewDecision,
    source_contracts: list[TaskContract],
    include_optional_findings: bool = False,
) -> list[GeneratedContract]:
    selected_findings = [
        finding
        for finding in decision.findings
        if finding.required_repairs or (include_optional_findings and finding.optional_follow_ups)
    ]
    if not selected_findings:
        return []

    unsafe_path_to_finding: dict[str, str] = {}
    generated: list[GeneratedContract] = []
    for index, finding in enumerate(selected_findings, start=1):
        scoped_finding = _repair_scope_finding(finding)
        mapped_contracts = _contracts_for_finding(scoped_finding, source_contracts=source_contracts)
        _record_unsafe_overlap(
            scoped_finding,
            unsafe_path_to_finding=unsafe_path_to_finding,
        )
        repair_contract = _build_repair_contract(
            decision=decision,
            finding=scoped_finding,
            mapped_contracts=mapped_contracts,
            index=index,
        )
        generated.append(
            GeneratedContract(
                task_id=repair_contract.task_id,
                title=repair_contract.title,
                objective=repair_contract.objective,
                rationale=f"Required feature-review finding {finding.finding_id} must be repaired before finalization.",
                suggested_contract=repair_contract,
            )
        )
    return generated


def classify_feature_review_findings_for_convergence(
    *,
    decision: FeatureReviewDecision,
    previous_decisions: list[FeatureReviewDecision],
    verification_passed: bool,
) -> FeatureReviewConvergenceResult:
    previous_findings = [finding for item in previous_decisions for finding in item.findings]
    classified: list[FeatureReviewFindingConvergenceResult] = []
    required_repair_ids: set[str] = set()
    for finding in decision.findings:
        if not finding.required_repairs and not finding.optional_follow_ups:
            raise FeatureReviewClassificationError(
                "feature review finding "
                f"{finding.finding_id} must include required_repairs or optional_follow_ups"
            )
        if finding.required_repairs:
            required_repair_ids.add(finding.finding_id)
        previous_match, repeated_by_id, adjacent_similarity = _match_previous_finding(
            finding=finding,
            previous_findings=previous_findings,
        )
        verification_false_positive_candidate = (
            verification_passed and bool(finding.required_repairs) and _is_verification_only_finding(finding)
        )
        if finding.required_repairs:
            result = FeatureReviewFindingConvergenceResult(
                finding_id=finding.finding_id,
                classification="blocker",
                selected_action="repair",
                matched_previous_finding_id=previous_match.finding_id if previous_match else None,
                repeated_by_finding_id=repeated_by_id,
                adjacent_similarity=adjacent_similarity,
                verification_false_positive_candidate=verification_false_positive_candidate,
            )
            classified.append(result)
            continue
        if finding.optional_follow_ups and previous_match is not None:
            classified.append(
                FeatureReviewFindingConvergenceResult(
                    finding_id=finding.finding_id,
                    classification="duplicate",
                    selected_action="defer",
                    matched_previous_finding_id=previous_match.finding_id,
                    repeated_by_finding_id=repeated_by_id,
                    adjacent_similarity=adjacent_similarity,
                    verification_false_positive_candidate=False,
                )
            )
            continue
        if finding.optional_follow_ups:
            if previous_decisions:
                if _has_file_overlap_with_any_previous_finding(
                    finding=finding,
                    previous_findings=previous_findings,
                ):
                    classification: FeatureReviewFindingClassification = "backlog_follow_up"
                else:
                    classification = "scope_expansion"
                classified.append(
                    FeatureReviewFindingConvergenceResult(
                        finding_id=finding.finding_id,
                        classification=classification,
                        selected_action="defer",
                        matched_previous_finding_id=None,
                        repeated_by_finding_id=False,
                        adjacent_similarity=0.0,
                        verification_false_positive_candidate=False,
                    )
                )
                continue
            classified.append(
                FeatureReviewFindingConvergenceResult(
                    finding_id=finding.finding_id,
                    classification="soft_finding",
                    selected_action="accept",
                    matched_previous_finding_id=None,
                    repeated_by_finding_id=False,
                    adjacent_similarity=0.0,
                    verification_false_positive_candidate=False,
                )
            )
            continue
        classified.append(
            FeatureReviewFindingConvergenceResult(
                finding_id=finding.finding_id,
                classification="false_positive",
                selected_action="accept",
                matched_previous_finding_id=previous_match.finding_id if previous_match else None,
                repeated_by_finding_id=repeated_by_id,
                adjacent_similarity=adjacent_similarity,
                verification_false_positive_candidate=False,
            )
        )

    _ensure_convergence_gate_consistency(classified=classified, required_repair_ids=required_repair_ids)
    return FeatureReviewConvergenceResult(
        findings=classified,
        # Hard gates are tied to required repairs, not to "accept" vs "defer" soft decisions.
        blocking_finding_ids=sorted(required_repair_ids),
        accepted_finding_ids=sorted(item.finding_id for item in classified if item.selected_action == "accept"),
        deferred_finding_ids=sorted(item.finding_id for item in classified if item.selected_action == "defer"),
        false_positive_candidate_ids=sorted(
            item.finding_id for item in classified if item.verification_false_positive_candidate
        ),
    )


def _repair_scope_finding(finding: FeatureReviewFinding) -> FeatureReviewFinding:
    editable_files = [
        path
        for path in finding.affected_files
        if not _is_generated_evidence_path(path)
    ]
    if editable_files == finding.affected_files:
        return finding
    if not editable_files:
        raise FeatureReviewContextError(
            f"finding {finding.finding_id} only references generated evidence paths; "
            "repair scope must name editable source or documentation files"
        )
    return finding.model_copy(update={"affected_files": editable_files})


def _ensure_git_ref(repo_path: Path, ref: str) -> None:
    try:
        _git_rev(repo_path, ref)
    except RuntimeError as error:
        raise FeatureReviewContextError(f"git ref not found: {ref}") from error


def _git_rev(repo_path: Path, ref: str) -> str:
    return git_text(repo_path, ["rev-parse", "--verify", ref]).strip()


def _latest_release_summary(
    *,
    runs_root: Path,
    release_id: str,
    integration_branch: str,
    integration_commit: str,
) -> tuple[Path | None, Path | None]:
    candidates = sorted(runs_root.glob(f"*_{release_id}_release/release_summary.json"))
    latest_run_dir: Path | None = None
    latest_summary_path: Path | None = None
    for summary_path in candidates:
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if summary.get("release_id") != release_id:
            continue
        if summary.get("integration_branch") != integration_branch:
            continue
        if summary.get("integration_commit") != integration_commit:
            continue
        latest_run_dir = summary_path.parent
        latest_summary_path = summary_path
    return latest_run_dir, latest_summary_path


def _safe_optional(path: Path, root: Path) -> Path | None:
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not resolved.exists():
        return None
    root_resolved = root.resolve()
    if resolved == root_resolved or root_resolved not in resolved.parents:
        raise FeatureReviewContextError(f"path escapes runs root: {path}")
    return resolved


def _safe_read_text(path: Path, *, allowed_roots: list[Path]) -> str:
    resolved = path.resolve()
    for root in allowed_roots:
        root_resolved = root.resolve()
        if resolved == root_resolved or root_resolved in resolved.parents:
            return resolved.read_text(encoding="utf-8", errors="replace")
    raise FeatureReviewContextError(f"path is outside allowed roots: {path}")


def _repo_relative(path: Path, repo_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(path)


def _safe_glob_files(root: Path, *, pattern: str) -> list[Path]:
    root_resolved = root.resolve()
    if not root_resolved.exists():
        return []
    files: list[Path] = []
    for path in root_resolved.glob(pattern):
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.is_file() and (resolved == root_resolved or root_resolved in resolved.parents):
            files.append(resolved)
    return files


def _render_docs_design_section(
    *,
    context: FeatureReviewContext,
    repo_root: Path,
    runs_root: Path,
    docs_design_root: Path,
) -> str:
    if not context.docs_design_paths:
        return "docs/design:\n- (missing)\n"

    relative_paths: list[str] = []
    for path in context.docs_design_paths:
        try:
            relative_paths.append(str(path.resolve().relative_to(docs_design_root.resolve())))
        except Exception:
            relative_paths.append(path.name)

    selected = [
        ("ARCHITECTURE.md", "docs/design/ARCHITECTURE.md"),
        ("TECHNICAL_SPECIFICATION.md", "docs/design/TECHNICAL_SPECIFICATION.md"),
        ("ROADMAP_AND_BACKLOG.md", "docs/design/ROADMAP_AND_BACKLOG.md"),
    ]
    excerpts: list[str] = []
    for filename, evidence_path in selected:
        path = docs_design_root / filename
        if not path.exists():
            continue
        text = _safe_read_text(path, allowed_roots=[repo_root, runs_root, docs_design_root])
        bounded = _bounded_review_text(
            text,
            max_chars=MAX_FEATURE_REVIEW_ARTIFACT_CHARS,
            evidence_path=evidence_path,
            label=filename,
        )
        excerpts.append(f"{evidence_path}:\n{bounded}".rstrip())

    return "\n".join(
        [
            "docs/design files:",
            *[f"- {path}" for path in relative_paths],
            "",
            "docs/design excerpts (bounded):",
            *(
                ["- (no excerptable design docs found)"]
                if not excerpts
                else [block for excerpt in excerpts for block in (excerpt, "")]
            ),
        ]
    ).rstrip()


def _collect_changed_file_excerpts(
    *,
    repo_path: Path,
    integration_branch: str,
    changed_files: list[str],
) -> list[tuple[str, str]]:
    excerpts: list[tuple[str, str]] = []
    for relative_path in changed_files[:MAX_FEATURE_REVIEW_CHANGED_FILE_EXCERPTS]:
        content = _git_show_file(repo_path, integration_branch=integration_branch, relative_path=relative_path)
        if content is None:
            continue
        bounded = _bounded_review_text(
            content,
            max_chars=MAX_FEATURE_REVIEW_CHANGED_FILE_EXCERPT_CHARS,
            evidence_path=f"git show {integration_branch}:{relative_path}",
            label=f"excerpt {relative_path}",
        )
        excerpts.append((relative_path, bounded))
    return excerpts


def _git_show_file(repo_path: Path, *, integration_branch: str, relative_path: str) -> str | None:
    normalized = relative_path.strip().lstrip("./")
    if not normalized:
        return None
    try:
        text = git_text(repo_path, ["show", f"{integration_branch}:{normalized}"])
    except RuntimeError:
        return None
    if "\x00" in text:
        return None
    return text


def _release_review_artifact_context(
    *,
    latest_release_run_dir: Path,
    runs_root: Path,
) -> tuple[list[str], Path | None, Path | None, Path | None, Path | None, Path | None]:
    summary_path = _safe_optional(latest_release_run_dir / "release_summary.json", runs_root)
    if summary_path is None:
        return [], None, None, None, None, None
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], None, None, None, None, None
    accepted_history: list[str] = []
    for item in payload.get("feature_review_proposals", []):
        if not isinstance(item, dict):
            continue
        finding_id = str(item.get("finding_id") or "").strip()
        classification = str(item.get("classification") or "").strip()
        action = str(item.get("selected_action") or "").strip()
        if not finding_id:
            continue
        accepted_history.append(
            f"finding_id={finding_id} classification={classification or 'unknown'} action={action or 'unknown'}"
        )
    prior_feature_review_path = _optional_runs_artifact_path(payload.get("feature_review_path"), runs_root)
    prior_feature_review_recheck_path = _optional_runs_artifact_path(
        payload.get("feature_review_recheck_path"), runs_root
    )
    final_integration_verification_path = _optional_runs_artifact_path(
        payload.get("final_integration_verification_path"), runs_root
    )
    final_integration_verification_log_path: Path | None = None
    final_integration_worktree_log_path: Path | None = None
    final_integration_verification = payload.get("final_integration_verification")
    if isinstance(final_integration_verification, dict):
        log_raw = final_integration_verification.get("verification_log_path")
        worktree_raw = final_integration_verification.get("worktree_log_path")
        final_integration_verification_log_path = _optional_runs_artifact_path(log_raw, runs_root)
        final_integration_worktree_log_path = _optional_runs_artifact_path(worktree_raw, runs_root)
    return (
        accepted_history,
        prior_feature_review_path,
        prior_feature_review_recheck_path,
        final_integration_verification_path,
        final_integration_verification_log_path,
        final_integration_worktree_log_path,
    )


def _optional_runs_artifact_path(raw_value: object, runs_root: Path) -> Path | None:
    if not isinstance(raw_value, str):
        return None
    cleaned = raw_value.strip()
    if not cleaned:
        return None
    try:
        return _safe_optional(Path(cleaned), runs_root)
    except FeatureReviewContextError:
        return None


def _parse_json_object(text: str) -> dict[str, Any]:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        loaded = None
    if isinstance(loaded, dict):
        return loaded

    start_index = None
    for index, char in enumerate(text):
        if char.isspace():
            continue
        if char != "{":
            raise ValueError("reviewer output contains non-whitespace before JSON object")
        start_index = index
        break

    if start_index is None:
        raise ValueError("reviewer output is empty")

    depth = 0
    in_string = False
    escape = False
    end_index = None
    for index in range(start_index, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
            continue
        if char == "}":
            depth -= 1
            if depth < 0:
                raise ValueError("reviewer output has an unmatched closing brace")
            if depth == 0:
                end_index = index + 1
                break
            continue

    if end_index is None or depth != 0:
        raise ValueError("reviewer output contains an unterminated JSON object")

    suffix = text[end_index:]
    if suffix.strip():
        raise ValueError("reviewer output contains non-whitespace after JSON object")

    loaded = json.loads(text[start_index:end_index])
    if not isinstance(loaded, dict):
        raise ValueError("parsed JSON is not an object")
    return loaded


def _process_error_message(stdout: str, stderr: str) -> str:
    message = stderr.strip() or stdout.strip()
    if not message:
        return "Reviewer backend failed with no output."
    return message


def _bounded_review_text(
    text: str,
    *,
    max_chars: int,
    evidence_path: str,
    label: str,
) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    omitted = len(text) - max_chars
    marker = (
        f"\n\n[feature review context truncated: {label} exceeded {max_chars} chars; "
        f"{omitted} chars omitted. Inspect full evidence at {evidence_path}.]\n\n"
    )
    return text[:head].rstrip() + marker + text[-tail:].lstrip()


def _blocked_feature_review_decision(
    *,
    release_id: str,
    reason: str,
    evidence_paths: list[Path],
) -> FeatureReviewDecision:
    remediation_hint = _blocked_feature_review_remediation_hint()
    finding = FeatureReviewFinding(
        finding_id=f"{release_id}:feature_review_blocked",
        severity=FeatureReviewSeverity.CRITICAL,
        summary=f"{reason.rstrip()}\n\n{remediation_hint}",
        affected_files=["feature_review_context"],
        evidence_paths=evidence_paths,
        required_repairs=[
            "Restore the reviewer backend and re-run feature review.",
            "If you do not want semantic review, unset `model_roles.reviewer` and re-run `run-release`.",
        ],
    )
    return FeatureReviewDecision(
        release_id=release_id,
        reviewer=Reviewer.DETERMINISTIC,
        summary="Feature review is blocked due to reviewer backend failure or invalid output.",
        recommendation=FeatureReviewRecommendation.ESCALATE,
        findings=[finding],
        accepted_risks=[],
        rerun_verification_commands=[],
    )


def _blocked_feature_review_remediation_hint() -> str:
    return "\n".join(
        [
            "Remediation hints:",
            "- Ensure the Codex CLI is installed and on PATH (`codex --version`).",
            "- Ensure the configured reviewer backend is supported (`executor.type: codex_cli`).",
            "- Verify `model_roles.reviewer` is configured only when you intend to run semantic feature review.",
            "- If the environment cannot run a reviewer backend, choose deterministic or human review for the release.",
        ]
    )


def _contracts_for_finding(
    finding: FeatureReviewFinding,
    *,
    source_contracts: list[TaskContract],
) -> list[TaskContract]:
    if not finding.affected_files:
        raise FeatureReviewContextError(
            f"finding {finding.finding_id} has no affected_files; cannot derive bounded repair scope"
        )
    matched: list[TaskContract] = []
    for contract in source_contracts:
        if any(
            any(_path_matches_allowed_pattern(path, pattern) for pattern in contract.allowed_files)
            for path in finding.affected_files
        ):
            matched.append(contract)
    unmapped = [
        path
        for path in finding.affected_files
        if not any(any(_path_matches_allowed_pattern(path, p) for p in c.allowed_files) for c in matched)
    ]
    if unmapped:
        raise FeatureReviewContextError(
            f"finding {finding.finding_id} references files outside source contract scope: {', '.join(unmapped)}"
        )
    return matched


def _path_matches_allowed_pattern(path: str, pattern: str) -> bool:
    normalized_path = path.strip().lstrip("./")
    normalized_pattern = pattern.strip().lstrip("./")
    if not normalized_path or not normalized_pattern:
        return False
    return fnmatchcase(normalized_path, normalized_pattern)


def _is_generated_evidence_path(path: str) -> bool:
    normalized = path.strip().lstrip("./")
    return normalized.startswith("runs/") or normalized.startswith("worktrees/")


def _is_unsafe_repair_path(path: str) -> bool:
    normalized = path.strip().lstrip("./").replace("\\", "/")
    if not normalized:
        return False
    candidate = PurePosixPath(normalized)
    if candidate.name in UNSAFE_REPAIR_FILENAMES:
        return True
    return any(part in UNSAFE_REPAIR_DIRNAMES for part in candidate.parts)


def _record_unsafe_overlap(
    finding: FeatureReviewFinding,
    *,
    unsafe_path_to_finding: dict[str, str],
) -> None:
    for path in finding.affected_files:
        normalized = path.strip().lstrip("./")
        if not _is_unsafe_repair_path(normalized):
            continue
        previous_finding = unsafe_path_to_finding.get(normalized)
        if previous_finding is not None and previous_finding != finding.finding_id:
            raise FeatureReviewContextError(
                f"unsafe path overlap across findings requires stop: {normalized} "
                f"({previous_finding}, {finding.finding_id})"
            )
        unsafe_path_to_finding[normalized] = finding.finding_id


def _build_repair_contract(
    *,
    decision: FeatureReviewDecision,
    finding: FeatureReviewFinding,
    mapped_contracts: list[TaskContract],
    index: int,
) -> TaskContract:
    allowed_files = _unique_strings(finding.affected_files)
    forbidden_changes = _unique_strings(
        item for contract in mapped_contracts for item in contract.forbidden_changes
    )
    required_evidence = _unique_strings(
        [
            "git diff",
            "changed-files list",
            *(
                item
                for contract in mapped_contracts
                for item in contract.required_evidence
            ),
        ]
    )
    verification_commands = _unique_strings(
        command
        for contract in mapped_contracts
        for command in contract.verification.commands
    )
    stop_conditions = _unique_strings(
        [
            *(
                condition
                for contract in mapped_contracts
                for condition in contract.stop_conditions
            ),
            "Stop if repair requires files outside mapped source contract scope.",
        ]
    )
    required_repairs = finding.required_repairs or [
        "Implement bounded repair for this required finding and rerun verification."
    ]
    return TaskContract.model_validate(
        {
            "task_id": f"{decision.release_id}-repair-{index:04d}",
            "release_id": decision.release_id,
            "title": f"Repair finding: {finding.summary[:80]}",
            "task_type": "code_only",
            "budget_class": mapped_contracts[0].budget_class,
            "objective": "; ".join(required_repairs),
            "allowed_files": allowed_files,
            "forbidden_changes": forbidden_changes,
            "required_evidence": required_evidence,
            "verification": {"commands": verification_commands},
            "stop_conditions": stop_conditions,
            "depends_on": [contract.task_id for contract in mapped_contracts],
        }
    )


def _unique_strings(values: Any) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _match_previous_finding(
    *,
    finding: FeatureReviewFinding,
    previous_findings: list[FeatureReviewFinding],
) -> tuple[FeatureReviewFinding | None, bool, float]:
    for previous in previous_findings:
        if previous.finding_id == finding.finding_id:
            return previous, True, 1.0

    best_match: FeatureReviewFinding | None = None
    best_similarity = 0.0
    for previous in previous_findings:
        file_overlap = _has_file_overlap(finding.affected_files, previous.affected_files)
        if not file_overlap:
            continue
        similarity = _summary_similarity(finding.summary, previous.summary)
        if similarity < 0.35:
            continue
        if similarity > best_similarity:
            best_match = previous
            best_similarity = similarity
    return best_match, False, best_similarity


def _has_file_overlap(current_files: list[str], previous_files: list[str]) -> bool:
    current = {item.strip().lstrip("./") for item in current_files if item.strip()}
    previous = {item.strip().lstrip("./") for item in previous_files if item.strip()}
    return bool(current.intersection(previous))


def _has_file_overlap_with_any_previous_finding(
    *,
    finding: FeatureReviewFinding,
    previous_findings: list[FeatureReviewFinding],
) -> bool:
    return any(
        _has_file_overlap(finding.affected_files, previous.affected_files)
        for previous in previous_findings
    )


def _summary_similarity(current: str, previous: str) -> float:
    current_tokens = _normalized_summary_tokens(current)
    previous_tokens = _normalized_summary_tokens(previous)
    if not current_tokens or not previous_tokens:
        return 0.0
    intersection = len(current_tokens.intersection(previous_tokens))
    union = len(current_tokens.union(previous_tokens))
    if union == 0:
        return 0.0
    return intersection / union


def _normalized_summary_tokens(summary: str) -> set[str]:
    token = []
    tokens: set[str] = set()
    for char in summary.lower():
        if char.isalnum():
            token.append(char)
            continue
        if token:
            tokens.add("".join(token))
            token = []
    if token:
        tokens.add("".join(token))
    return tokens


def _is_verification_only_finding(finding: FeatureReviewFinding) -> bool:
    if not finding.evidence_paths:
        return False
    verification_markers = ("verification", "pytest", "test", "junit")
    return all(any(marker in str(path).lower() for marker in verification_markers) for path in finding.evidence_paths)


def _ensure_convergence_gate_consistency(
    *,
    classified: list[FeatureReviewFindingConvergenceResult],
    required_repair_ids: set[str],
) -> None:
    for item in classified:
        if item.finding_id in required_repair_ids and item.selected_action != "repair":
            raise FeatureReviewContextError(
                f"required repair finding {item.finding_id} cannot be classified with action {item.selected_action}"
            )
        if item.selected_action == "defer" and item.classification not in {
            "duplicate",
            "backlog_follow_up",
            "scope_expansion",
        }:
            raise FeatureReviewContextError(
                f"deferred finding {item.finding_id} must be duplicate/backlog_follow_up/scope_expansion"
            )
