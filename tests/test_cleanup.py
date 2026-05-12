from __future__ import annotations

import subprocess
from pathlib import Path

from agentic_devloop.cleanup import cleanup_release_artifacts


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout


def _write_config(config_dir: Path, repo: Path, worktree_root: Path) -> None:
    config_dir.mkdir()
    (config_dir / "demo.yaml").write_text(
        f"""
project_id: demo
repo_path: {repo}
default_base_branch: main
worktree_root: {worktree_root}

executor:
  type: codex_cli
  model: gpt-5.3-codex-spark
  max_walltime_minutes: 10

verification_profiles:
  default:
    commands:
      - "true"

budget:
  max_executor_attempts_per_task: 1
  max_strong_model_calls_per_release: 1
  max_changed_files_per_task: 5
  max_diff_lines_per_task: 100
""".lstrip(),
        encoding="utf-8",
    )


def _create_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")


def test_cleanup_release_artifacts_dry_run_reports_candidates(tmp_path) -> None:
    repo = tmp_path / "repo"
    worktree_root = tmp_path / "worktrees"
    config_dir = tmp_path / "configs"
    _create_repo(repo)
    _write_config(config_dir, repo, worktree_root)
    worktree_root.mkdir()
    stale_worktree = worktree_root / "20260512T120000Z_v1.0.0_demo-0001"
    stale_worktree.mkdir()
    _git(repo, "branch", "agent/v1.0.0/demo-0001")
    _git(repo, "branch", "feature/v1.0.0")

    report = cleanup_release_artifacts(
        project_id="demo",
        release_id="v1.0.0",
        config_dir=config_dir,
        include_integration_branch=True,
    )

    assert report.dry_run is True
    assert report.worktree_paths == [stale_worktree]
    assert report.task_branches == ["agent/v1.0.0/demo-0001"]
    assert report.integration_branch == "feature/v1.0.0"
    assert stale_worktree.exists()
    assert "agent/v1.0.0/demo-0001" in _git(repo, "branch", "--format=%(refname:short)")


def test_cleanup_release_artifacts_force_removes_stale_artifacts(tmp_path) -> None:
    repo = tmp_path / "repo"
    worktree_root = tmp_path / "worktrees"
    config_dir = tmp_path / "configs"
    _create_repo(repo)
    _write_config(config_dir, repo, worktree_root)
    worktree_root.mkdir()
    stale_worktree = worktree_root / "20260512T120000Z_v1.0.0_demo-0001"
    stale_worktree.mkdir()
    ignored_worktree = worktree_root / "20260512T120000Z_other_demo-0001"
    ignored_worktree.mkdir()
    _git(repo, "branch", "agent/v1.0.0/demo-0001")
    _git(repo, "branch", "feature/v1.0.0")

    report = cleanup_release_artifacts(
        project_id="demo",
        release_id="v1.0.0",
        config_dir=config_dir,
        force=True,
        include_integration_branch=True,
    )

    branches = _git(repo, "branch", "--format=%(refname:short)")
    assert report.dry_run is False
    assert report.errors == []
    assert report.removed_worktrees == [stale_worktree]
    assert report.deleted_branches == ["agent/v1.0.0/demo-0001", "feature/v1.0.0"]
    assert not stale_worktree.exists()
    assert ignored_worktree.exists()
    assert "agent/v1.0.0/demo-0001" not in branches
    assert "feature/v1.0.0" not in branches
