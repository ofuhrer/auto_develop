from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from fnmatch import fnmatchcase
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
    GeneratedContract,
    Reviewer,
    TaskContract,
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


UNSAFE_REPAIR_PATH_MARKERS: tuple[str, ...] = (
    "poetry.lock",
    "uv.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "migrations/",
    "generated/",
)

MAX_FEATURE_REVIEW_DIFF_CHARS = 120_000
MAX_FEATURE_REVIEW_ARTIFACT_CHARS = 40_000


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
        integration_commit=integration_commit,
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

    if config.type != "codex_cli":
        error_message = (
            f"Unsupported feature review executor backend: {config.type}. "
            "Only codex_cli is supported for feature review."
        )
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(error_message + "\n", encoding="utf-8")
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
                    "stderr_chars": len(error_message) + 1,
                    "error": error_message,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        decision = _blocked_feature_review_decision(
            release_id=release_id,
            reason=error_message,
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
    finding = FeatureReviewFinding(
        finding_id=f"{release_id}:feature_review_blocked",
        severity=FeatureReviewSeverity.CRITICAL,
        summary=reason,
        affected_files=["feature_review_context"],
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


def _record_unsafe_overlap(
    finding: FeatureReviewFinding,
    *,
    unsafe_path_to_finding: dict[str, str],
) -> None:
    for path in finding.affected_files:
        normalized = path.strip().lstrip("./")
        if not any(marker in normalized for marker in UNSAFE_REPAIR_PATH_MARKERS):
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
