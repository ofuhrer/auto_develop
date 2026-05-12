from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from agentic_devloop.evidence import EvidenceCollector, write_review_decision
from agentic_devloop.models import ExecutorResult, TaskContract, TaskRun, TaskState
from agentic_devloop.review import deterministic_review
from agentic_devloop.yaml_io import load_yaml_model


ROOT = Path(__file__).resolve().parents[1]


def _git(repo, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_evidence_collector_writes_complete_bundle(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    (repo / "README.md").write_text("# changed\n", encoding="utf-8")

    task = load_yaml_model(ROOT / "contracts" / "rr-0001.yaml", TaskContract)
    now = datetime.now(timezone.utc)
    run_state = TaskRun(
        task_id=task.task_id,
        state=TaskState.REVIEWING,
        worktree_path=repo,
        branch="main",
        executor_attempts=1,
        started_at=now,
        updated_at=now,
        changed_files=["README.md"],
        diff_lines=2,
        verification_results=[],
    )

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    prompt_path = source_dir / "executor_prompt.md"
    stdout_path = source_dir / "executor_stdout.log"
    stderr_path = source_dir / "executor_stderr.log"
    verification_log_path = source_dir / "verification.log"
    prompt_path.write_text("Do the task.\n", encoding="utf-8")
    stdout_path.write_text("stdout\n", encoding="utf-8")
    stderr_path.write_text("stderr\n", encoding="utf-8")
    verification_log_path.write_text("verification\n", encoding="utf-8")

    executor_result = ExecutorResult(
        command=["codex"],
        exit_code=0,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        duration_seconds=1.0,
        backend="codex_cli",
        model="gpt-5.3-codex-spark",
    )

    bundle = EvidenceCollector().collect(
        run_id="2026-05-12_v0.8.0",
        task=task,
        run_state=run_state,
        worktree_path=repo,
        bundle_path=tmp_path / "runs" / "rr-0001",
        contract_source_path=ROOT / "contracts" / "rr-0001.yaml",
        executor_prompt_path=prompt_path,
        executor_result=executor_result,
        verification_log_path=verification_log_path,
    )

    assert bundle.contract_path.exists()
    assert bundle.run_state_path.exists()
    assert bundle.git_diff_path.read_text(encoding="utf-8")
    assert bundle.changed_files_path.read_text(encoding="utf-8") == "README.md\n"

    decision = deterministic_review(
        task=task,
        budget=task_budget(),
        changed_files=["tests/test_public_real_site_conditional_pilot_run.py"],
        diff_text="+assert report\n",
        verification_exit_codes=[0],
    )
    updated_bundle = write_review_decision(bundle, decision)

    assert updated_bundle.decision_path is not None
    assert updated_bundle.decision_path.exists()
    assert updated_bundle.review_path is not None
    assert updated_bundle.review_path.exists()


def task_budget():
    from agentic_devloop.models import Budget

    return Budget(
        max_executor_attempts_per_task=2,
        max_strong_model_calls_per_release=10,
        max_changed_files_per_task=8,
        max_diff_lines_per_task=600,
    )
