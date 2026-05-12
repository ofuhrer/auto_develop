from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agentic_devloop.process import run_process


class GitFinalizeError(RuntimeError):
    def __init__(self, message: str, *, step: str = "git") -> None:
        super().__init__(message)
        self.step = step


@dataclass(frozen=True)
class FinalizeResult:
    commit_hash: str | None = None
    merged: bool = False
    pushed: bool = False
    failed_step: str | None = None
    error: str | None = None
    lock_path: str | None = None
    rebased_onto: str | None = None


@dataclass(frozen=True)
class MergeLockStatus:
    path: Path
    exists: bool
    stale: bool
    pid: int | None = None
    created_at: str | None = None


def commit_worktree_changes(worktree_path: Path, message: str) -> str | None:
    if not _has_changes(worktree_path):
        return None

    _git(worktree_path, ["add", "--all"])
    _git(worktree_path, ["commit", "-m", message])
    return _git(worktree_path, ["rev-parse", "HEAD"]).strip()


def merge_branch(repo_path: Path, branch: str) -> None:
    _ensure_clean(repo_path)
    _git(repo_path, ["merge", "--no-edit", branch], step="merge")


def push_branch(repo_path: Path, branch: str, remote: str = "origin") -> None:
    _git(repo_path, ["push", remote, branch])


def ensure_branch_from_base(repo_path: Path, branch: str, base_branch: str) -> None:
    _ensure_clean(repo_path)
    if _branch_exists(repo_path, branch):
        return
    # Only assert that the base ref exists here; callers decide whether they also want to
    # switch the checked-out branch before later merge or finalization steps.
    _require_branch(repo_path, base_branch, step="branch")
    _git(repo_path, ["branch", branch, base_branch])


def merge_integration_branch_to_base(
    *,
    repo_path: Path,
    integration_branch: str,
    base_branch: str,
    push: bool = False,
) -> FinalizeResult:
    lock_path = repo_path / ".git" / "agent-main.lock"
    with _merge_lock(lock_path):
        _ensure_base_branch(repo_path, base_branch)
        rebased_onto = _git(repo_path, ["rev-parse", base_branch]).strip()
        merge_branch(repo_path, integration_branch)
        pushed = False
        if push:
            push_branch(repo_path, base_branch)
            pushed = True
        return FinalizeResult(
            merged=True,
            pushed=pushed,
            lock_path=str(lock_path),
            rebased_onto=rebased_onto,
        )


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
        raise GitFinalizeError(f"repository has uncommitted changes: {repo_path}", step="clean-check")


def _ensure_base_branch(repo_path: Path, base_branch: str) -> None:
    _ensure_clean(repo_path)
    current_branch = _git(repo_path, ["branch", "--show-current"]).strip()
    if current_branch != base_branch:
        _git(repo_path, ["switch", base_branch])


def _rebase_worktree_onto(worktree_path: Path, base_branch: str) -> None:
    _ensure_clean(worktree_path)
    upstream = base_branch
    if _has_remote(worktree_path, "origin"):
        remote_ref = f"origin/{base_branch}"
        fetch_result = run_process(
            ["git", "fetch", "origin", base_branch],
            cwd=worktree_path,
            timeout_seconds=120,
        )
        if fetch_result.exit_code == 0 and _ref_exists(worktree_path, remote_ref):
            upstream = remote_ref
    _git(worktree_path, ["rebase", upstream], step="rebase")


def _has_remote(repo_path: Path, remote: str) -> bool:
    result = run_process(["git", "remote"], cwd=repo_path, timeout_seconds=120)
    if result.exit_code != 0:
        return False
    return remote in result.stdout.splitlines()


def _branch_exists(repo_path: Path, branch: str) -> bool:
    return _ref_exists(repo_path, branch)


def _require_branch(repo_path: Path, branch: str, *, step: str) -> None:
    if not _branch_exists(repo_path, branch):
        raise GitFinalizeError(f"base branch not found: {branch}", step=step)


def _ref_exists(repo_path: Path, ref: str) -> bool:
    result = run_process(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=repo_path,
        timeout_seconds=120,
    )
    return result.exit_code == 0


class _merge_lock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "_merge_lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(
                self.fd,
                (
                    f"pid={os.getpid()}\n"
                    f"created_at={datetime.now(UTC).isoformat()}\n"
                ).encode(),
            )
        except FileExistsError as error:
            status = inspect_merge_lock(self.path.parent.parent)
            if status.stale:
                raise GitFinalizeError(
                    f"stale merge lock present: {self.path}. Run cleanup --force to recover it.",
                    step="lock",
                ) from error
            raise GitFinalizeError(f"merge lock already held: {self.path}", step="lock") from error
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def abort_rebase(worktree_path: Path) -> None:
    run_process(["git", "rebase", "--abort"], cwd=worktree_path, timeout_seconds=120)


def continue_rebase(worktree_path: Path) -> None:
    _git(worktree_path, ["-c", "core.editor=true", "rebase", "--continue"], step="rebase-continue")


def merge_lock_path(repo_path: Path) -> Path:
    return repo_path / ".git" / "agent-main.lock"


def inspect_merge_lock(repo_path: Path) -> MergeLockStatus:
    path = merge_lock_path(repo_path)
    if not path.exists():
        return MergeLockStatus(path=path, exists=False, stale=False)
    data = _read_lock_metadata(path)
    pid = _parse_pid(data.get("pid"))
    return MergeLockStatus(
        path=path,
        exists=True,
        stale=pid is not None and not _pid_exists(pid),
        pid=pid,
        created_at=data.get("created_at"),
    )


def recover_merge_lock(repo_path: Path) -> Path | None:
    status = inspect_merge_lock(repo_path)
    if not status.exists or not status.stale:
        return None
    status.path.unlink(missing_ok=True)
    return status.path


def _git(repo_path: Path, args: list[str], *, step: str = "git") -> str:
    result = run_process(["git", *args], cwd=repo_path, timeout_seconds=120)
    if result.exit_code != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise GitFinalizeError(message, step=step)
    return result.stdout


def _read_lock_metadata(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value
    return data


def _parse_pid(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
