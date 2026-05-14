from __future__ import annotations

from pathlib import Path

import pytest

from agentic_devloop.config import (
    ProjectConfigError,
    discover_safe_verification_runtime,
    load_project_config,
)


ROOT = Path(__file__).resolve().parents[1]


def test_load_project_config() -> None:
    config = load_project_config("auto_develop", ROOT / "configs")

    assert config.project_id == "auto_develop"
    assert config.default_base_branch == "main"
    assert config.repo_state_path == ROOT / "repo_state" / "auto_develop"


def test_load_project_config_resolves_repo_state_from_target_repo_when_controller_state_is_absent(
    tmp_path,
) -> None:
    repo = tmp_path / "target"
    repo_state = repo / "repo_state" / "demo"
    repo_state.mkdir(parents=True)
    config_dir = tmp_path / "controller" / "configs"
    config_dir.mkdir(parents=True)
    (config_dir / "demo.yaml").write_text(
        f"""
project_id: demo
repo_path: {repo}
default_base_branch: main
worktree_root: {tmp_path / "worktrees"}
repo_state_path: repo_state/demo
executor:
  type: codex_cli
  model: worker
  max_walltime_minutes: 5
verification_profiles:
  default:
    commands:
      - "true"
budget:
  max_executor_attempts_per_task: 2
  max_strong_model_calls_per_release: 10
  max_changed_files_per_task: 8
  max_diff_lines_per_task: 600
""".lstrip(),
        encoding="utf-8",
    )

    config = load_project_config("demo", config_dir)

    assert config.repo_state_path == repo_state


def test_missing_repo_fails_when_repo_validation_is_requested() -> None:
    config_dir = ROOT / "tests" / "fixtures" / "configs"
    with pytest.raises(ProjectConfigError, match="repo path does not exist"):
        load_project_config("missing_repo", config_dir, validate_repo=True)


def test_load_project_config_rejects_repo_wide_unsafe_overlap_paths(tmp_path) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    (config_dir / "demo.yaml").write_text(
        f"""
project_id: demo
repo_path: {repo}
default_base_branch: main
worktree_root: {tmp_path / "worktrees"}
unsafe_overlap_paths:
  - "**"
executor:
  type: codex_cli
  model: worker
  max_walltime_minutes: 5
verification_profiles:
  default:
    commands:
      - "true"
budget:
  max_executor_attempts_per_task: 2
  max_strong_model_calls_per_release: 10
  max_changed_files_per_task: 8
  max_diff_lines_per_task: 600
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsafe overlap paths must not include repo-wide globs"):
        load_project_config("demo", config_dir)


def test_load_project_config_rejects_invalid_release_finalization_policy(tmp_path) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    (config_dir / "demo.yaml").write_text(
        f"""
project_id: demo
repo_path: {repo}
default_base_branch: main
worktree_root: {tmp_path / "worktrees"}
release_finalization_policy:
  policy: push-feature
executor:
  type: codex_cli
  model: worker
  max_walltime_minutes: 5
verification_profiles:
  default:
    commands:
      - "true"
budget:
  max_executor_attempts_per_task: 2
  max_strong_model_calls_per_release: 10
  max_changed_files_per_task: 8
  max_diff_lines_per_task: 600
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Input should be"):
        load_project_config("demo", config_dir)


def test_discover_safe_verification_runtime_prefers_explicit_runtime(tmp_path) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    (config_dir / "demo.yaml").write_text(
        f"""
project_id: demo
repo_path: {repo}
default_base_branch: main
worktree_root: {tmp_path / "worktrees"}
verification_runtime:
  python_path: /shared/.venv/bin/python
  env:
    PYTHONPATH: src
executor:
  type: codex_cli
  model: worker
  max_walltime_minutes: 5
verification_profiles:
  default:
    commands:
      - ".venv/bin/python -m pytest"
budget:
  max_executor_attempts_per_task: 2
  max_strong_model_calls_per_release: 10
  max_changed_files_per_task: 8
  max_diff_lines_per_task: 600
""".lstrip(),
        encoding="utf-8",
    )

    config = load_project_config("demo", config_dir)

    assert discover_safe_verification_runtime(config) == "/shared/.venv/bin/python"


def test_load_project_config_rejects_non_positive_feature_review_max_repair_loops(tmp_path) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    (config_dir / "demo.yaml").write_text(
        f"""
project_id: demo
repo_path: {repo}
default_base_branch: main
worktree_root: {tmp_path / "worktrees"}
feature_review_max_repair_loops: 0
executor:
  type: codex_cli
  model: worker
  max_walltime_minutes: 5
verification_profiles:
  default:
    commands:
      - "true"
budget:
  max_executor_attempts_per_task: 2
  max_strong_model_calls_per_release: 10
  max_changed_files_per_task: 8
  max_diff_lines_per_task: 600
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="feature_review_max_repair_loops"):
        load_project_config("demo", config_dir)
