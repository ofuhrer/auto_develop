from __future__ import annotations

import shutil
from dataclasses import dataclass, field
import json
from pathlib import Path

from agentic_devloop.config import load_project_config
from agentic_devloop.process import run_process
from agentic_devloop.release import feature_branch_name


@dataclass(frozen=True)
class CleanupReport:
    project_id: str
    release_id: str
    dry_run: bool
    worktree_paths: list[Path] = field(default_factory=list)
    task_branches: list[str] = field(default_factory=list)
    integration_branch: str | None = None
    eligible_worktree_paths: list[Path] = field(default_factory=list)
    skipped_worktree_paths: list[dict[str, str]] = field(default_factory=list)
    eligible_branches: list[str] = field(default_factory=list)
    skipped_branches: list[dict[str, str]] = field(default_factory=list)
    finalization_evidence_path: Path | None = None
    removed_worktrees: list[Path] = field(default_factory=list)
    deleted_branches: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def cleanup_release_artifacts(
    *,
    project_id: str,
    release_id: str,
    config_dir: Path = Path("configs"),
    force: bool = False,
    include_integration_branch: bool = False,
    runs_dir: Path = Path("runs"),
) -> CleanupReport:
    config = load_project_config(project_id, config_dir, validate_repo=True)
    repo_path = config.repo_path
    worktree_root = config.worktree_root
    worktree_paths = _release_worktree_paths(worktree_root, release_id)
    task_branches = _matching_branches(repo_path, f"agent/{release_id}/*")
    integration_branch = feature_branch_name(release_id)
    integration_branch_candidate = integration_branch if include_integration_branch else None
    release_summary_path = _find_release_summary_path(runs_dir=runs_dir, release_id=release_id)
    evidence_path = _find_finalization_decision_path(release_summary_path)
    finalization_evidence = _load_json(evidence_path)

    if integration_branch_candidate and not _branch_exists(repo_path, integration_branch_candidate):
        integration_branch_candidate = None

    skipped_worktree_paths: list[dict[str, str]] = []
    eligible_worktree_paths: list[Path] = []
    eligible_branches: list[str] = []
    skipped_branches: list[dict[str, str]] = []

    for worktree_path in worktree_paths:
        reason = _worktree_skip_reason(worktree_root=worktree_root, worktree_path=worktree_path, release_id=release_id)
        if reason is None:
            eligible_worktree_paths.append(worktree_path)
        else:
            skipped_worktree_paths.append({"path": str(worktree_path), "reason": reason})

    for branch in task_branches:
        reason = _branch_skip_reason(
            repo_path=repo_path,
            branch=branch,
            release_id=release_id,
            base_branch=config.default_base_branch,
            integration_branch=integration_branch,
            include_integration_branch=include_integration_branch,
            finalization_evidence=finalization_evidence,
        )
        if reason is None:
            eligible_branches.append(branch)
        else:
            skipped_branches.append({"branch": branch, "reason": reason})

    if integration_branch_candidate is not None:
        reason = _integration_branch_skip_reason(
            repo_path=repo_path,
            branch=integration_branch_candidate,
            base_branch=config.default_base_branch,
            finalization_evidence=finalization_evidence,
        )
        if reason is None:
            eligible_branches.append(integration_branch_candidate)
        else:
            skipped_branches.append({"branch": integration_branch_candidate, "reason": reason})

    if not force:
        return CleanupReport(
            project_id=project_id,
            release_id=release_id,
            dry_run=True,
            worktree_paths=worktree_paths,
            task_branches=task_branches,
            integration_branch=integration_branch_candidate,
            eligible_worktree_paths=eligible_worktree_paths,
            skipped_worktree_paths=skipped_worktree_paths,
            eligible_branches=eligible_branches,
            skipped_branches=skipped_branches,
            finalization_evidence_path=evidence_path,
        )

    removed_worktrees: list[Path] = []
    deleted_branches: list[str] = []
    errors: list[str] = []

    for worktree_path in eligible_worktree_paths:
        error = _remove_worktree(repo_path, worktree_root, worktree_path)
        if error is None:
            removed_worktrees.append(worktree_path)
        else:
            errors.append(error)

    for branch in eligible_branches:
        error = _delete_branch(repo_path, branch)
        if error is None:
            deleted_branches.append(branch)
        else:
            errors.append(error)

    return CleanupReport(
        project_id=project_id,
        release_id=release_id,
        dry_run=False,
        worktree_paths=worktree_paths,
        task_branches=task_branches,
        integration_branch=integration_branch_candidate,
        eligible_worktree_paths=eligible_worktree_paths,
        skipped_worktree_paths=skipped_worktree_paths,
        eligible_branches=eligible_branches,
        skipped_branches=skipped_branches,
        finalization_evidence_path=evidence_path,
        removed_worktrees=removed_worktrees,
        deleted_branches=deleted_branches,
        errors=errors,
    )


def _release_worktree_paths(worktree_root: Path, release_id: str) -> list[Path]:
    if not worktree_root.exists():
        return []
    return sorted(
        path
        for path in worktree_root.iterdir()
        if path.is_dir() and not path.name.startswith(".") and release_id in path.name
    )


def _matching_branches(repo_path: Path, pattern: str) -> list[str]:
    result = run_process(
        ["git", "branch", "--list", pattern, "--format=%(refname:short)"],
        cwd=repo_path,
        timeout_seconds=60,
    )
    if result.exit_code != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return sorted(line.strip() for line in result.stdout.splitlines() if line.strip())


def _find_release_summary_path(*, runs_dir: Path, release_id: str) -> Path | None:
    if not runs_dir.exists():
        return None
    candidates = sorted(
        runs_dir.glob(f"*_{release_id}_release/release_summary.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _find_finalization_decision_path(summary_path: Path | None) -> Path | None:
    if summary_path is None:
        return None
    summary = _load_json(summary_path)
    if not isinstance(summary, dict):
        return None
    value = summary.get("finalization_decision_path")
    if not isinstance(value, str) or not value.strip():
        return None
    configured_path = Path(value)
    if configured_path.is_absolute() or configured_path.exists():
        return configured_path
    sibling_path = summary_path.parent / configured_path.name
    if sibling_path.exists():
        return sibling_path
    return configured_path


def _load_json(path: Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _branch_exists(repo_path: Path, branch: str) -> bool:
    result = run_process(
        ["git", "rev-parse", "--verify", "--quiet", branch],
        cwd=repo_path,
        timeout_seconds=60,
    )
    return result.exit_code == 0


def _delete_branch(repo_path: Path, branch: str) -> str | None:
    current_branch = _current_branch(repo_path)
    if current_branch == branch:
        return f"refusing_to_delete_current_branch={branch}"

    result = run_process(["git", "branch", "-D", branch], cwd=repo_path, timeout_seconds=120)
    if result.exit_code == 0:
        return None
    if "not found" in result.stderr.lower():
        return None
    return f"delete_branch_failed={branch}: {result.stderr.strip() or result.stdout.strip()}"


def _branch_skip_reason(
    *,
    repo_path: Path,
    branch: str,
    release_id: str,
    base_branch: str,
    integration_branch: str,
    include_integration_branch: bool,
    finalization_evidence: dict[str, object] | None,
) -> str | None:
    if not branch.startswith(f"agent/{release_id}/"):
        return "non_release_owned_branch"
    if _current_branch(repo_path) == branch:
        return "current_branch"

    merge_target = integration_branch if include_integration_branch and _branch_exists(repo_path, integration_branch) else base_branch
    if not _is_merged_into(repo_path, branch, merge_target):
        return f"unmerged_into_{merge_target}"

    if finalization_evidence is not None:
        decision = finalization_evidence.get("decision")
        if isinstance(decision, str) and decision != "accepted":
            return f"release_decision_{decision}"
    return None


def _integration_branch_skip_reason(
    *,
    repo_path: Path,
    branch: str,
    base_branch: str,
    finalization_evidence: dict[str, object] | None,
) -> str | None:
    if _current_branch(repo_path) == branch:
        return "current_branch"
    if _is_merged_into(repo_path, branch, base_branch):
        return None
    if _evidence_marks_integration_branch_eligible(finalization_evidence):
        return None
    return f"integration_branch_not_merged_into_{base_branch}"


def _evidence_marks_integration_branch_eligible(finalization_evidence: dict[str, object] | None) -> bool:
    if not isinstance(finalization_evidence, dict):
        return False
    if finalization_evidence.get("outcome") == "executed":
        finalization = finalization_evidence.get("finalization")
        if isinstance(finalization, dict):
            if finalization.get("merged") is True:
                return True
        policy = finalization_evidence.get("policy")
        if isinstance(policy, dict) and policy.get("policy") == "local_merge":
            return True
    finalization = finalization_evidence.get("finalization")
    if isinstance(finalization, dict):
        if finalization.get("merged") is True:
            return True
        mode = finalization.get("mode")
        if mode in {"merge-main", "push-main"} and finalization.get("blocked") is not True:
            return True
    finalization_result = finalization_evidence.get("finalization_result")
    if isinstance(finalization_result, dict):
        gate = finalization_result.get("gate")
        if isinstance(gate, dict) and gate.get("allowed") is True:
            action = finalization_result.get("action")
            if action in {"merge-main", "push-main"}:
                return True
    return False


def _worktree_skip_reason(*, worktree_root: Path, worktree_path: Path, release_id: str) -> str | None:
    if not _is_inside(worktree_path, worktree_root):
        return "outside_worktree_root"
    if release_id not in worktree_path.name:
        return "non_release_worktree"
    return None


def _current_branch(repo_path: Path) -> str:
    result = run_process(["git", "branch", "--show-current"], cwd=repo_path, timeout_seconds=60)
    if result.exit_code != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def _is_merged_into(repo_path: Path, branch: str, target_branch: str) -> bool:
    if not _branch_exists(repo_path, target_branch):
        return False
    result = run_process(
        ["git", "merge-base", "--is-ancestor", branch, target_branch],
        cwd=repo_path,
        timeout_seconds=60,
    )
    return result.exit_code == 0


def _remove_worktree(repo_path: Path, worktree_root: Path, worktree_path: Path) -> str | None:
    if not _is_inside(worktree_path, worktree_root):
        return f"refusing_to_remove_path_outside_worktree_root={worktree_path}"

    result = run_process(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=repo_path,
        timeout_seconds=120,
    )
    if result.exit_code == 0 or not worktree_path.exists():
        return None

    # Stale debug directories are not always registered as Git worktrees.
    shutil.rmtree(worktree_path)
    return None


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True
