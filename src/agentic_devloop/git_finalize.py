from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentic_devloop.process import run_process


class GitFinalizeError(RuntimeError):
    pass


@dataclass(frozen=True)
class FinalizeResult:
    commit_hash: str | None = None
    merged: bool = False
    pushed: bool = False
    failed_step: str | None = None
    error: str | None = None


def commit_worktree_changes(worktree_path: Path, message: str) -> str | None:
    if not _has_changes(worktree_path):
        return None

    _git(worktree_path, ["add", "--all"])
    _git(worktree_path, ["commit", "-m", message])
    return _git(worktree_path, ["rev-parse", "HEAD"]).strip()


def merge_branch(repo_path: Path, branch: str) -> None:
    _ensure_clean(repo_path)
    _git(repo_path, ["merge", "--no-edit", branch])


def push_branch(repo_path: Path, branch: str, remote: str = "origin") -> None:
    _git(repo_path, ["push", remote, branch])


def finalize_accepted_task(
    *,
    repo_path: Path,
    worktree_path: Path,
    task_branch: str,
    base_branch: str,
    commit_message: str,
    merge: bool,
    push: bool,
) -> FinalizeResult:
    commit_hash = commit_worktree_changes(worktree_path, commit_message)

    merged = False
    pushed = False
    if merge:
        _ensure_base_branch(repo_path, base_branch)
        merge_branch(repo_path, task_branch)
        merged = True

    if push:
        push_branch(repo_path, base_branch)
        pushed = True

    return FinalizeResult(commit_hash=commit_hash, merged=merged, pushed=pushed)


def _has_changes(repo_path: Path) -> bool:
    return bool(_git(repo_path, ["status", "--porcelain", "--untracked-files=all"]).strip())


def _ensure_clean(repo_path: Path) -> None:
    if _has_changes(repo_path):
        raise GitFinalizeError(f"repository has uncommitted changes: {repo_path}")


def _ensure_base_branch(repo_path: Path, base_branch: str) -> None:
    _ensure_clean(repo_path)
    current_branch = _git(repo_path, ["branch", "--show-current"]).strip()
    if current_branch != base_branch:
        _git(repo_path, ["switch", base_branch])


def _git(repo_path: Path, args: list[str]) -> str:
    result = run_process(["git", *args], cwd=repo_path, timeout_seconds=120)
    if result.exit_code != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise GitFinalizeError(message)
    return result.stdout
