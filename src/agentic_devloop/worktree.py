from __future__ import annotations

from pathlib import Path

from agentic_devloop.process import run_process


class WorktreeError(RuntimeError):
    pass


def _git(repo_path: Path, args: list[str]) -> str:
    result = run_process(["git", *args], cwd=repo_path, timeout_seconds=60)
    if result.exit_code != 0:
        raise WorktreeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def ensure_clean_repo(repo_path: Path) -> None:
    status = _git(repo_path, ["status", "--porcelain"])
    if status.strip():
        raise WorktreeError("base repository has uncommitted changes")


def create_worktree(
    *,
    repo_path: Path,
    worktree_path: Path,
    branch: str,
    base_branch: str,
    allow_dirty: bool = False,
) -> Path:
    if not allow_dirty:
        ensure_clean_repo(repo_path)

    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo_path, ["worktree", "add", "-b", branch, str(worktree_path), base_branch])
    return worktree_path


def remove_worktree(repo_path: Path, worktree_path: Path, *, force: bool = False) -> None:
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(worktree_path))
    _git(repo_path, args)
