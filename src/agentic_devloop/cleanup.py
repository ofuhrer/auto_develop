from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from agentic_devloop.config import load_project_config
from agentic_devloop.git_finalize import inspect_merge_lock, recover_merge_lock
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
    stale_lock_paths: list[Path] = field(default_factory=list)
    removed_worktrees: list[Path] = field(default_factory=list)
    deleted_branches: list[str] = field(default_factory=list)
    removed_lock_paths: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def cleanup_release_artifacts(
    *,
    project_id: str,
    release_id: str,
    config_dir: Path = Path("configs"),
    force: bool = False,
    include_integration_branch: bool = False,
) -> CleanupReport:
    config = load_project_config(project_id, config_dir, validate_repo=True)
    repo_path = config.repo_path
    worktree_root = config.worktree_root
    worktree_paths = _release_worktree_paths(worktree_root, release_id)
    task_branches = _matching_branches(repo_path, f"agent/{release_id}/*")
    integration_branch = feature_branch_name(release_id)
    integration_branch_candidate = integration_branch if include_integration_branch else None
    merge_lock = inspect_merge_lock(repo_path)
    stale_lock_paths = [merge_lock.path] if merge_lock.exists and merge_lock.stale else []

    if integration_branch_candidate and not _branch_exists(repo_path, integration_branch_candidate):
        integration_branch_candidate = None

    if not force:
        return CleanupReport(
            project_id=project_id,
            release_id=release_id,
            dry_run=True,
            worktree_paths=worktree_paths,
            task_branches=task_branches,
            integration_branch=integration_branch_candidate,
            stale_lock_paths=stale_lock_paths,
        )

    removed_worktrees: list[Path] = []
    deleted_branches: list[str] = []
    removed_lock_paths: list[Path] = []
    errors: list[str] = []

    for worktree_path in worktree_paths:
        error = _remove_worktree(repo_path, worktree_root, worktree_path)
        if error is None:
            removed_worktrees.append(worktree_path)
        else:
            errors.append(error)

    for branch in [*task_branches, *([integration_branch_candidate] if integration_branch_candidate else [])]:
        error = _delete_branch(repo_path, branch)
        if error is None:
            deleted_branches.append(branch)
        else:
            errors.append(error)
    recovered_lock = recover_merge_lock(repo_path)
    if recovered_lock is not None:
        removed_lock_paths.append(recovered_lock)

    return CleanupReport(
        project_id=project_id,
        release_id=release_id,
        dry_run=False,
        worktree_paths=worktree_paths,
        task_branches=task_branches,
        integration_branch=integration_branch_candidate,
        stale_lock_paths=stale_lock_paths,
        removed_worktrees=removed_worktrees,
        deleted_branches=deleted_branches,
        removed_lock_paths=removed_lock_paths,
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


def _current_branch(repo_path: Path) -> str:
    result = run_process(["git", "branch", "--show-current"], cwd=repo_path, timeout_seconds=60)
    if result.exit_code != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


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
