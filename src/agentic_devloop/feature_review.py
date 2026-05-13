from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_devloop.config import load_project_config
from agentic_devloop.git_state import git_text
from agentic_devloop.models import (
    ExecutorConfig,
    FeatureReviewDecision,
    FeatureReviewFinding,
    FeatureReviewRecommendation,
    FeatureReviewSeverity,
    Reviewer,
)
from agentic_devloop.process import run_process


class FeatureReviewContextError(ValueError):
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


@dataclass(frozen=True)
class FeatureReviewBackendResult:
    decision: FeatureReviewDecision
    prompt_path: Path
    stdout_path: Path
    stderr_path: Path
    metadata_path: Path
    raw_output: str


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
) -> FeatureReviewContext:
    repo_path = repo_path.resolve()
    runs_root = (repo_path / runs_dir).resolve()
    docs_design_root = (repo_path / docs_design_dir).resolve()

    _ensure_git_ref(repo_path, base_branch)
    _ensure_git_ref(repo_path, integration_branch)

    base_commit = _git_rev(repo_path, base_branch)
    integration_commit = _git_rev(repo_path, integration_branch)
    diff_text = git_text(repo_path, ["diff", "--patch", f"{base_branch}..{integration_branch}"])
    changed_files = [
        line
        for line in git_text(repo_path, ["diff", "--name-only", f"{base_branch}..{integration_branch}"])
        .splitlines()
        if line.strip()
    ]

    docs_design_paths = sorted(_safe_glob_files(docs_design_root, pattern="**/*"))

    latest_release_run_dir, summary_path = _latest_release_summary(
        runs_root=runs_root,
        release_id=release_id,
        integration_branch=integration_branch,
    )
    if latest_release_run_dir is None:
        release_review_path = None
        metrics_path = None
        budget_path = None
        tuning_path = None
    else:
        release_review_path = _safe_optional(latest_release_run_dir / "release_review.md", runs_root)
        metrics_path = _safe_optional(latest_release_run_dir / "release_metrics.json", runs_root)
        budget_path = _safe_optional(latest_release_run_dir / "release_budget.json", runs_root)
        tuning_path = _safe_optional(latest_release_run_dir / "release_tuning.md", runs_root)

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
        return _safe_read_text(path, allowed_roots=[repo_root, runs_root, docs_design_root])

    docs_section = _render_docs_design_section(context, docs_design_root)
    release_summary = read(context.release_summary_path)
    release_review = read(context.release_review_path)
    release_metrics = read(context.release_metrics_path)
    release_budget = read(context.release_budget_path)
    release_tuning = read(context.release_tuning_path)

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
            "",
            "Changed files (base..integration):",
            *([f"- {path}" for path in context.changed_files] or ["- (none)"]),
            "",
            "Git diff (base..integration):",
            context.diff_text,
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
                "backend": config.type,
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


def _render_docs_design_section(context: FeatureReviewContext, docs_design_root: Path) -> str:
    if not context.docs_design_paths:
        return "docs/design:\n- (missing)\n"
    relative = []
    for path in context.docs_design_paths:
        try:
            relative.append(str(path.resolve().relative_to(docs_design_root.resolve())))
        except Exception:
            relative.append(path.name)
    return "\n".join(["docs/design files:", *[f"- {path}" for path in relative]])


def _parse_json_object(text: str) -> dict[str, Any]:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        loaded = None
    if isinstance(loaded, dict):
        return loaded
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("no JSON object found in output")
    loaded = json.loads(match.group(0))
    if not isinstance(loaded, dict):
        raise ValueError("parsed JSON is not an object")
    return loaded


def _process_error_message(stdout: str, stderr: str) -> str:
    message = stderr.strip() or stdout.strip()
    if not message:
        return "Reviewer backend failed with no output."
    return message


def _blocked_feature_review_decision(
    *,
    release_id: str,
    reason: str,
    evidence_paths: list[Path],
) -> FeatureReviewDecision:
    finding = FeatureReviewFinding(
        finding_id=f"{release_id}:feature_review_blocked",
        severity=FeatureReviewSeverity.CRITICAL,
        summary=reason,
        evidence_paths=evidence_paths,
        required_repairs=["Restore reviewer backend or provide missing context and retry feature review."],
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
