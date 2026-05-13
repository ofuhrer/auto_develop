from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agentic_devloop.cleanup import _remove_worktree, cleanup_release_artifacts


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
    assert report.eligible_worktree_paths == [stale_worktree]
    assert report.eligible_branches == ["agent/v1.0.0/demo-0001", "feature/v1.0.0"]
    assert report.skipped_branches == []
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


def test_cleanup_release_artifacts_refuses_current_branch_deletion(tmp_path) -> None:
    repo = tmp_path / "repo"
    worktree_root = tmp_path / "worktrees"
    config_dir = tmp_path / "configs"
    _create_repo(repo)
    _write_config(config_dir, repo, worktree_root)
    _git(repo, "checkout", "-b", "feature/v1.0.0")
    _git(repo, "branch", "agent/v1.0.0/demo-0001")

    report = cleanup_release_artifacts(
        project_id="demo",
        release_id="v1.0.0",
        config_dir=config_dir,
        force=True,
        include_integration_branch=True,
    )

    branches = _git(repo, "branch", "--format=%(refname:short)")
    assert report.deleted_branches == ["agent/v1.0.0/demo-0001"]
    assert {"branch": "feature/v1.0.0", "reason": "current_branch"} in report.skipped_branches
    assert "feature/v1.0.0" in branches


def test_cleanup_release_artifacts_refuses_unmerged_integration_branch_deletion(tmp_path) -> None:
    repo = tmp_path / "repo"
    worktree_root = tmp_path / "worktrees"
    config_dir = tmp_path / "configs"
    _create_repo(repo)
    _write_config(config_dir, repo, worktree_root)
    _git(repo, "checkout", "-b", "feature/v1.0.0")
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature only")
    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", "agent/v1.0.0/demo-0001")
    (repo / "task.txt").write_text("task\n", encoding="utf-8")
    _git(repo, "add", "task.txt")
    _git(repo, "commit", "-m", "task only")
    _git(repo, "checkout", "main")

    report = cleanup_release_artifacts(
        project_id="demo",
        release_id="v1.0.0",
        config_dir=config_dir,
        force=True,
        include_integration_branch=True,
    )

    branches = _git(repo, "branch", "--format=%(refname:short)")
    assert report.deleted_branches == []
    assert {"branch": "agent/v1.0.0/demo-0001", "reason": "unmerged_into_feature/v1.0.0"} in report.skipped_branches
    assert {
        "branch": "feature/v1.0.0",
        "reason": "integration_branch_not_merged_into_main",
    } in report.skipped_branches
    assert "feature/v1.0.0" in branches
    assert "agent/v1.0.0/demo-0001" in branches


def test_cleanup_uses_finalization_decision_referenced_by_release_summary(tmp_path) -> None:
    repo = tmp_path / "repo"
    worktree_root = tmp_path / "worktrees"
    config_dir = tmp_path / "configs"
    runs_dir = tmp_path / "runs"
    _create_repo(repo)
    _write_config(config_dir, repo, worktree_root)
    _git(repo, "checkout", "-b", "feature/v1.0.0")
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature only")
    _git(repo, "checkout", "main")
    release_run_dir = runs_dir / "20260513T120000Z_v1.0.0_release"
    release_run_dir.mkdir(parents=True)
    decision_path = release_run_dir / "finalization_decision.json"
    decision_path.write_text(
        json.dumps(
            {
                "release_id": "v1.0.0",
                "run_id": "20260513T120000Z_v1.0.0",
                "requested_mode": "merge-main",
                "policy": {"policy": "local_merge", "required_credential_env_vars": []},
                "policy_source": "config",
                "gate": {"allowed": True, "reason": "ok", "decision": "accepted"},
                "outcome": "executed",
                "stop_reason": None,
                "missing_credentials": [],
                "git_commands": ["git merge --no-edit feature/v1.0.0 (into main)"],
                "handoff_path": None,
                "finalization": {"merged": True, "pushed": False, "failed_step": None, "error": None},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (release_run_dir / "release_summary.json").write_text(
        json.dumps(
            {
                "run_id": "20260513T120000Z_v1.0.0",
                "release_id": "v1.0.0",
                "decision": "accepted",
                "finalization_decision_path": str(decision_path),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = cleanup_release_artifacts(
        project_id="demo",
        release_id="v1.0.0",
        config_dir=config_dir,
        include_integration_branch=True,
        runs_dir=runs_dir,
    )

    assert report.finalization_evidence_path == decision_path
    assert report.eligible_branches == ["feature/v1.0.0"]
    assert report.skipped_branches == []


def test_remove_worktree_refuses_path_outside_worktree_root(tmp_path) -> None:
    repo = tmp_path / "repo"
    _create_repo(repo)
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    error = _remove_worktree(repo, worktree_root, outside)

    assert error == f"refusing_to_remove_path_outside_worktree_root={outside}"
