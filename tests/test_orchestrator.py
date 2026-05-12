from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import yaml

from agentic_devloop.models import ExecutorResult
from agentic_devloop.orchestrator import run_task


class FakeExecutor:
    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = worktree_path / "docs" / "result.md"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("# Result\n\nImplemented by fake executor.\n", encoding="utf-8")

        stdout_path = output_dir / "executor_stdout.log"
        stderr_path = output_dir / "executor_stderr.log"
        stdout_path.write_text(f"used prompt {prompt_path}\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")

        return ExecutorResult(
            command=["fake-executor"],
            exit_code=0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            duration_seconds=0.01,
            backend="fake",
            model=None,
        )


class FailingExecutor:
    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = output_dir / "executor_stdout.log"
        stderr_path = output_dir / "executor_stderr.log"
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("usage limit\n", encoding="utf-8")

        return ExecutorResult(
            command=["fake-executor"],
            exit_code=2,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            duration_seconds=0.01,
            backend="fake",
            model=None,
        )


def _git(repo, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_run_task_wires_executor_verification_evidence_and_review(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

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
                "model": "gpt-5.3-codex-spark",
                "max_walltime_minutes": 5,
            },
            "verification_profiles": {"default": {"commands": ["test -f docs/result.md"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )

    contract_path = tmp_path / "contract.yaml"
    _write_yaml(
        contract_path,
        {
            "task_id": "demo-0001",
            "release_id": "v0.1.0",
            "title": "Create docs result",
            "budget_class": "S",
            "objective": "Create a result document.",
            "allowed_files": ["docs/**"],
            "forbidden_changes": ["Do not edit source code."],
            "required_evidence": ["git diff", "test output"],
            "verification": {"commands": ["test -f docs/result.md"]},
            "stop_conditions": ["Verification fails twice."],
        },
    )

    result = run_task(
        project_id="demo",
        contract_path=contract_path,
        config_dir=config_dir,
        runs_dir=tmp_path / "runs",
        executor=FakeExecutor(),
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    assert result.decision.decision == "accepted"
    assert (result.bundle_path / "executor_prompt.md").exists()
    assert (result.bundle_path / "decision.yaml").exists()
    assert (result.bundle_path / "review.md").exists()
    assert (result.bundle_path / "changed_files.txt").read_text(encoding="utf-8") == "docs/result.md\n"
    assert "+Implemented by fake executor." in (result.bundle_path / "git_diff.patch").read_text(
        encoding="utf-8"
    )


def test_run_task_can_commit_merge_and_push_accepted_changes(tmp_path) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")

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
                "model": "gpt-5.3-codex-spark",
                "max_walltime_minutes": 5,
            },
            "verification_profiles": {"default": {"commands": ["test -f docs/result.md"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )

    contract_path = tmp_path / "contract.yaml"
    _write_yaml(
        contract_path,
        {
            "task_id": "demo-0003",
            "release_id": "v0.1.0",
            "title": "Create docs result",
            "budget_class": "S",
            "objective": "Create a result document.",
            "allowed_files": ["docs/**"],
            "forbidden_changes": [],
            "required_evidence": ["git diff", "test output"],
            "verification": {"commands": ["test -f docs/result.md"]},
            "stop_conditions": ["Verification fails twice."],
        },
    )

    result = run_task(
        project_id="demo",
        contract_path=contract_path,
        config_dir=config_dir,
        runs_dir=tmp_path / "runs",
        executor=FakeExecutor(),
        now=datetime(2026, 5, 12, 12, 2, tzinfo=UTC),
        push_on_accept=True,
        commit_message="Add generated docs result",
    )

    assert result.decision.decision == "accepted"
    assert result.finalize is not None
    assert result.finalize.commit_hash is not None
    assert result.finalize.merged is True
    assert result.finalize.pushed is True
    assert (repo / "docs" / "result.md").read_text(encoding="utf-8").startswith("# Result")

    remote_main = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert remote_main == result.finalize.commit_hash


def test_run_task_escalates_executor_failure_without_verification(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

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
                "model": "gpt-5.3-codex-spark",
                "max_walltime_minutes": 5,
            },
            "verification_profiles": {"default": {"commands": ["false"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )
    contract_path = tmp_path / "contract.yaml"
    _write_yaml(
        contract_path,
        {
            "task_id": "demo-0002",
            "release_id": "v0.1.0",
            "title": "Fail before verification",
            "budget_class": "S",
            "objective": "Do not reach verification.",
            "allowed_files": ["README.md"],
            "forbidden_changes": [],
            "required_evidence": ["git diff", "test output"],
            "verification": {"commands": ["false"]},
            "stop_conditions": ["Verification fails twice."],
        },
    )

    result = run_task(
        project_id="demo",
        contract_path=contract_path,
        config_dir=config_dir,
        runs_dir=tmp_path / "runs",
        executor=FailingExecutor(),
        now=datetime(2026, 5, 12, 12, 1, tzinfo=UTC),
    )

    assert result.decision.decision == "escalated"
    assert result.decision.rationale == "Executor failed with exit code 2."
    assert (
        result.bundle_path / "verification.log"
    ).read_text(encoding="utf-8") == "Verification skipped because executor failed.\n"


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
