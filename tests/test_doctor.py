from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from agentic_devloop.doctor import run_doctor


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_run_doctor_reports_repo_git_worktree_release_and_model_risks(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    _git(repo, "branch", "feature/v1.0.0")
    _git(repo, "branch", "agent/v1.0.0/demo-0001")

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {
                "type": "codex_cli",
                "model": "gpt-5.3-codex",
                "max_walltime_minutes": 5,
            },
            "model_catalog": {
                "coding_worker": {
                    "model": "gpt-5.3-codex",
                    "capabilities": ["implementation"],
                    "budget_class": "M",
                    "availability": "unsupported",
                },
                "micro_repair": {
                    "model": "gpt-5.3-codex-spark",
                    "capabilities": ["conflict_repair"],
                    "budget_class": "XS",
                    "availability": "unknown",
                },
                "cheap_router": {
                    "model": "gpt-5.4-mini",
                    "capabilities": ["fallback_worker"],
                    "budget_class": "S",
                    "availability": "supported",
                },
            },
            "model_roles": {
                "worker": {
                    "type": "codex_cli",
                    "model": "gpt-5.3-codex",
                    "fallback_models": ["gpt-5.4-mini"],
                    "max_walltime_minutes": 5,
                },
                "repair": {
                    "type": "codex_cli",
                    "model": "gpt-5.3-codex-spark",
                    "fallback_models": ["gpt-5.4-mini"],
                    "max_walltime_minutes": 5,
                },
            },
            "model_routing": {
                "default_role": "worker",
                "escalation_role": "repair",
            },
            "verification_profiles": {
                "default": {
                    "commands": ["cargo fmt --check", "cargo test --all-targets --all-features"],
                }
            },
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )
    worktree_root = tmp_path / "worktrees"
    (worktree_root / "stale-release-worktree").mkdir(parents=True)

    report = run_doctor(project_id="demo", config_dir=config_dir, release_id="v1.0.0")
    payload = report.to_dict()

    assert payload["project_id"] == "demo"
    assert payload["repo_path"] == str(repo)
    assert payload["repo_exists"] is True
    assert payload["repo_is_git_repo"] is True
    assert payload["current_branch"] == "main"
    assert payload["dirty"] is True
    assert payload["dirty_files"] == ["dirty.txt"]
    assert payload["worktree_root"]["clean"] is False
    assert payload["worktree_root"]["entries"] == [str(worktree_root / "stale-release-worktree")]
    assert payload["verification_profiles"]["default"] == [
        "cargo fmt --check",
        "cargo test --all-targets --all-features",
    ]
    assert payload["model_routing"]["resolved_roles"]["worker"]["model"] == "gpt-5.3-codex"
    assert payload["model_routing"]["resolved_roles"]["worker"]["catalog"]["availability"] == "unsupported"
    assert payload["release"]["integration_branch"] == ["feature/v1.0.0"]
    assert payload["release"]["task_branches"] == ["agent/v1.0.0/demo-0001"]

    messages = [diagnostic["message"] for diagnostic in payload["diagnostics"]]
    assert any("uncommitted changes" in message for message in messages)
    assert any("stale entries" in message for message in messages)
    assert any("integration branch already exists" in message for message in messages)
    assert any("task branches already exist" in message for message in messages)
    assert any("primary model gpt-5.3-codex, which model_catalog marks unsupported" in message for message in messages)
    assert any("primary model gpt-5.3-codex-spark, which model_catalog marks unknown" in message for message in messages)
    assert not any("role worker has no confirmed supported fallback" in message for message in messages)


def _write_yaml(path: Path, data: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
