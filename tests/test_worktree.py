from __future__ import annotations

import subprocess

import pytest

from agentic_devloop.worktree import WorktreeError, create_worktree, ensure_clean_repo


def _git(repo, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_worktree_manager_creates_isolated_worktree(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

    worktree_path = tmp_path / "worktrees" / "task-1"

    created = create_worktree(
        repo_path=repo,
        worktree_path=worktree_path,
        branch="agent/task-1",
        base_branch="main",
    )

    assert created == worktree_path
    assert (worktree_path / "README.md").exists()


def test_clean_repo_check_rejects_dirty_repo(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("# dirty\n", encoding="utf-8")

    with pytest.raises(WorktreeError, match="uncommitted changes"):
        ensure_clean_repo(repo)
