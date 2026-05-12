from __future__ import annotations

import os
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
    lock_path: str | None = None
    rebased_onto: str | None = None


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
    lock_path = repo_path / ".git" / "agent-main.lock"
    with _merge_lock(lock_path):
        commit_hash = commit_worktree_changes(worktree_path, commit_message)

        merged = False
        pushed = False
        rebased_onto = None
        if merge:
            _ensure_base_branch(repo_path, base_branch)
            rebased_onto = _git(repo_path, ["rev-parse", base_branch]).strip()
            _rebase_worktree_onto(worktree_path, base_branch)
            merge_branch(repo_path, task_branch)
            merged = True

        if push:
            push_branch(repo_path, base_branch)
            pushed = True

        return FinalizeResult(
            commit_hash=commit_hash,
            merged=merged,
            pushed=pushed,
            lock_path=str(lock_path),
            rebased_onto=rebased_onto,
        )


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


def _rebase_worktree_onto(worktree_path: Path, base_branch: str) -> None:
    _ensure_clean(worktree_path)
    upstream = base_branch
    if _has_remote(worktree_path, "origin"):
        _git(worktree_path, ["fetch", "origin", base_branch])
        remote_ref = f"origin/{base_branch}"
        try:
            _git(worktree_path, ["rev-parse", "--verify", remote_ref])
            upstream = remote_ref
        except GitFinalizeError:
            upstream = base_branch
    _git(worktree_path, ["rebase", upstream])


def _has_remote(repo_path: Path, remote: str) -> bool:
    result = run_process(["git", "remote"], cwd=repo_path, timeout_seconds=120)
    if result.exit_code != 0:
        return False
    return remote in result.stdout.splitlines()


class _merge_lock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "_merge_lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, f"pid={os.getpid()}\n".encode())
        except FileExistsError as error:
            raise GitFinalizeError(f"merge lock already held: {self.path}") from error
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _git(repo_path: Path, args: list[str]) -> str:
    result = run_process(["git", *args], cwd=repo_path, timeout_seconds=120)
    if result.exit_code != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise GitFinalizeError(message)
    return result.stdout
