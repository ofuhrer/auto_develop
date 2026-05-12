from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from agentic_devloop.config import load_project_config
from agentic_devloop.evidence import EvidenceCollector, write_review_decision
from agentic_devloop.executor import CodexExecutor
from agentic_devloop.git_state import changed_files as git_changed_files
from agentic_devloop.git_state import diff_patch
from agentic_devloop.models import (
    ExecutorResult,
    ProjectConfig,
    ReviewDecision,
    TaskContract,
    TaskRun,
    TaskState,
)
from agentic_devloop.prompt import write_executor_prompt
from agentic_devloop.review import deterministic_review
from agentic_devloop.verification import VerificationRunner
from agentic_devloop.worktree import create_worktree
from agentic_devloop.yaml_io import load_yaml_model


class ExecutorProtocol(Protocol):
    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        ...


@dataclass(frozen=True)
class TaskRunResult:
    run_id: str
    worktree_path: Path
    bundle_path: Path
    decision: ReviewDecision


def make_run_id(release_id: str, task_id: str, now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{release_id}_{task_id}"


def branch_name(release_id: str, task_id: str) -> str:
    return f"agent/{release_id}/{task_id}"


def run_task(
    *,
    project_id: str,
    contract_path: Path,
    config_dir: Path = Path("configs"),
    runs_dir: Path = Path("runs"),
    executor: ExecutorProtocol | None = None,
    verification_timeout_seconds: int = 600,
    allow_dirty: bool = False,
    now: datetime | None = None,
) -> TaskRunResult:
    config = load_project_config(project_id, config_dir, validate_repo=True)
    task = load_yaml_model(contract_path, TaskContract)
    run_id = make_run_id(task.release_id, task.task_id, now)
    branch = branch_name(task.release_id, task.task_id)
    worktree_path = config.worktree_root / run_id
    run_root = runs_dir / run_id / task.task_id
    scratch_dir = run_root / "_scratch"
    bundle_path = run_root / "evidence"

    started_at = datetime.now(UTC)
    create_worktree(
        repo_path=config.repo_path,
        worktree_path=worktree_path,
        branch=branch,
        base_branch=config.default_base_branch,
        allow_dirty=allow_dirty,
    )

    prompt_path = write_executor_prompt(task, scratch_dir / "executor_prompt.md")
    task_run = TaskRun(
        task_id=task.task_id,
        state=TaskState.EXECUTING,
        worktree_path=worktree_path,
        branch=branch,
        executor_attempts=1,
        started_at=started_at,
        updated_at=datetime.now(UTC),
        changed_files=[],
        diff_lines=0,
        verification_results=[],
    )

    selected_executor = executor or _executor_for_config(config)
    executor_result = selected_executor.run(
        prompt_path=prompt_path,
        worktree_path=worktree_path,
        output_dir=scratch_dir,
    )

    verification_results = VerificationRunner(timeout_seconds=verification_timeout_seconds).run(
        commands=task.verification.commands,
        worktree_path=worktree_path,
        output_dir=scratch_dir,
    )

    current_diff = diff_patch(worktree_path)
    current_changed_files = git_changed_files(worktree_path)
    task_run = task_run.model_copy(
        update={
            "state": TaskState.REVIEWING,
            "updated_at": datetime.now(UTC),
            "changed_files": current_changed_files,
            "diff_lines": _review_line_count(current_diff),
            "verification_results": verification_results,
        }
    )

    bundle = EvidenceCollector().collect(
        run_id=run_id,
        task=task,
        run_state=task_run,
        worktree_path=worktree_path,
        bundle_path=bundle_path,
        contract_source_path=contract_path,
        executor_prompt_path=prompt_path,
        executor_result=executor_result,
        verification_log_path=scratch_dir / "verification.log",
    )
    decision = deterministic_review(
        task=task,
        budget=config.budget,
        changed_files=current_changed_files,
        diff_text=current_diff,
        verification_exit_codes=[result.exit_code for result in verification_results],
    )
    write_review_decision(bundle, decision)

    return TaskRunResult(
        run_id=run_id,
        worktree_path=worktree_path,
        bundle_path=bundle.bundle_path,
        decision=decision,
    )


def _executor_for_config(config: ProjectConfig) -> ExecutorProtocol:
    if config.executor.type != "codex_cli":
        raise ValueError(f"unsupported executor type: {config.executor.type}")
    return CodexExecutor(config.executor)


def _review_line_count(diff: str) -> int:
    return sum(1 for line in diff.splitlines() if line.startswith(("+", "-")))
