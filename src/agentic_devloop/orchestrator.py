from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Protocol

from agentic_devloop.config import load_project_config
from agentic_devloop.context import enforce_context_budget, load_context_bundle
from agentic_devloop.conflict_repair import conflicted_files, write_conflict_repair_prompt
from agentic_devloop.evidence import (
    EvidenceCollector,
    write_conflict_repair_result,
    write_failure_diagnosis,
    write_finalization_result,
    write_review_decision,
    write_scientific_outputs,
)
from agentic_devloop.executor import CodexExecutor
from agentic_devloop.failure_diagnosis import (
    DeterministicFailureDiagnosisBackend,
    FailureDiagnosisBackend,
    FailureDiagnosisRequest,
)
from agentic_devloop.git_finalize import (
    FinalizeResult,
    GitFinalizeError,
    continue_rebase,
    finalize_accepted_task,
)
from agentic_devloop.git_state import changed_files as git_changed_files
from agentic_devloop.git_state import diff_patch
from agentic_devloop.models import (
    CommandResult,
    Decision,
    ConflictRepairResult,
    EvidenceBundle,
    ExecutorAttempt,
    ExecutorConfig,
    ExecutorResult,
    FailureDiagnosis,
    ProjectConfig,
    Reviewer,
    ReviewDecision,
    TaskContract,
    TaskRun,
    TaskState,
)
from agentic_devloop.prompt import write_executor_prompt
from agentic_devloop.review import deterministic_review
from agentic_devloop.scientific import analyze_scientific_changes
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
    finalize: FinalizeResult | None = None


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
    commit_on_accept: bool = False,
    merge_on_accept: bool = False,
    push_on_accept: bool = False,
    commit_message: str | None = None,
    base_branch: str | None = None,
    now: datetime | None = None,
    progress: Callable[[str], None] | None = None,
    failure_diagnosis_backend: FailureDiagnosisBackend | None = None,
) -> TaskRunResult:
    config = load_project_config(project_id, config_dir, validate_repo=True)
    task = load_yaml_model(contract_path, TaskContract)
    diagnosis_backend = failure_diagnosis_backend or DeterministicFailureDiagnosisBackend()
    run_id = make_run_id(task.release_id, task.task_id, now)
    branch = branch_name(task.release_id, task.task_id)
    worktree_path = config.worktree_root / run_id
    run_root = runs_dir / run_id / task.task_id
    scratch_dir = run_root / "_scratch"
    bundle_path = run_root / "evidence"
    task_base_branch = base_branch or config.default_base_branch

    _report(progress, f"event=task_run_created task={task.task_id} run_id={run_id}")
    _report(progress, f"event=worktree_created task={task.task_id} path={worktree_path}")
    started_at = datetime.now(UTC)
    create_worktree(
        repo_path=config.repo_path,
        worktree_path=worktree_path,
        branch=branch,
        base_branch=task_base_branch,
        allow_dirty=allow_dirty,
    )

    _report(progress, f"event=prompt_build_started task={task.task_id}")
    context = load_context_bundle(config, task)
    enforce_context_budget(context, config.budget.max_context_chars_per_task)
    _report(progress, f"event=context_loaded task={task.task_id} sections={len(context.sections)} chars={context.total_chars}")
    prompt_path = write_executor_prompt(task, scratch_dir / "executor_prompt.md", context)
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

    executor_configs = executor_configs_for_task(config, task)
    executor_result = _run_executor_attempts(
        task_id=task.task_id,
        executor_configs=executor_configs,
        executor=executor,
        max_attempts=config.budget.max_executor_attempts_per_task,
        prompt_path=prompt_path,
        worktree_path=worktree_path,
        scratch_dir=scratch_dir,
        progress=progress,
    )
    _report(progress, f"event=executor_finished task={task.task_id} exit_code={executor_result.exit_code}")
    if executor_result.exit_code != 0:
        verification_log_path = scratch_dir / "verification.log"
        verification_log_path.write_text(
            "Verification skipped because executor failed.\n",
            encoding="utf-8",
        )
        current_diff = diff_patch(worktree_path)
        current_changed_files = git_changed_files(worktree_path)
        task_run = task_run.model_copy(
            update={
                "state": TaskState.ESCALATED,
                "updated_at": datetime.now(UTC),
                "changed_files": current_changed_files,
                "diff_lines": _review_line_count(current_diff),
                "verification_results": [],
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
            verification_log_path=verification_log_path,
        )
        decision = ReviewDecision(
            task_id=task.task_id,
            decision=Decision.ESCALATED,
            reviewer=Reviewer.DETERMINISTIC,
            rationale=f"Executor failed with exit code {executor_result.exit_code}.",
        )
        bundle = _diagnose_failure(
            bundle=bundle,
            task=task,
            executor_result=executor_result,
            verification_results=[],
            changed_files=current_changed_files,
            verification_log_path=verification_log_path,
            backend=diagnosis_backend,
            progress=progress,
        )
        write_review_decision(bundle, decision)
        _report(progress, f"event=review_decision task={task.task_id} decision={decision.decision} rationale={json.dumps(decision.rationale)}")

        return TaskRunResult(
            run_id=run_id,
            worktree_path=worktree_path,
            bundle_path=bundle.bundle_path,
            decision=decision,
        )

    verification_commands = _verification_commands(config, task)
    _report(progress, f"event=verification_started task={task.task_id} commands={len(verification_commands)}")
    verification_results = VerificationRunner(timeout_seconds=verification_timeout_seconds).run(
        commands=verification_commands,
        worktree_path=worktree_path,
        output_dir=scratch_dir,
    )
    _report(
        progress,
        f"event=verification_finished task={task.task_id} exit_codes="
        + ",".join(str(result.exit_code) for result in verification_results),
    )

    _report(progress, f"event=evidence_collection_started task={task.task_id}")
    current_diff = diff_patch(worktree_path)
    current_changed_files = git_changed_files(worktree_path)
    scientific_review = analyze_scientific_changes(
        task=task,
        changed_files=current_changed_files,
        diff_text=current_diff,
    )
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
        scientific_review=scientific_review,
    )
    bundle = write_scientific_outputs(bundle, task, scientific_review)
    if any(result.exit_code != 0 for result in verification_results):
        bundle = _diagnose_failure(
            bundle=bundle,
            task=task,
            executor_result=executor_result,
            verification_results=verification_results,
            changed_files=current_changed_files,
            verification_log_path=bundle.verification_log_path,
            backend=diagnosis_backend,
            progress=progress,
        )
    write_review_decision(bundle, decision)
    _report(progress, f"event=review_decision task={task.task_id} decision={decision.decision} rationale={json.dumps(decision.rationale)}")
    finalize_result = None
    if decision.decision == Decision.ACCEPTED and (
        commit_on_accept or merge_on_accept or push_on_accept
    ):
        should_merge = merge_on_accept or push_on_accept
        should_push = push_on_accept
        message = commit_message or f"{task.task_id}: {task.title}"
        _report(progress, f"event=task_finalization_started task={task.task_id}")
        try:
            finalize_result = finalize_accepted_task(
                repo_path=config.repo_path,
                worktree_path=worktree_path,
                task_branch=branch,
                base_branch=task_base_branch,
                commit_message=message,
                merge=should_merge,
                push=should_push,
            )
            if finalize_result.commit_hash:
                _report(progress, f"event=task_committed task={task.task_id} commit={finalize_result.commit_hash}")
            if finalize_result.merged:
                _report(progress, f"event=task_merged task={task.task_id} target={task_base_branch}")
            if finalize_result.pushed:
                _report(progress, f"event=task_pushed task={task.task_id} branch=origin/{task_base_branch}")
        except GitFinalizeError as error:
            _report(progress, f"event=task_finalization_failed task={task.task_id} error={json.dumps(str(error))}")
            repair_result = _attempt_conflict_repair(
                error=error,
                task=task,
                executor_configs=executor_configs,
                executor=executor,
                worktree_path=worktree_path,
                scratch_dir=scratch_dir,
                verification_commands=verification_commands,
                verification_timeout_seconds=verification_timeout_seconds,
                progress=progress,
            )
            bundle = write_conflict_repair_result(bundle, repair_result)
            if repair_result.resolved:
                try:
                    finalize_result = finalize_accepted_task(
                        repo_path=config.repo_path,
                        worktree_path=worktree_path,
                        task_branch=branch,
                        base_branch=task_base_branch,
                        commit_message=message,
                        merge=should_merge,
                        push=should_push,
                    )
                except GitFinalizeError as retry_error:
                    finalize_result = _failed_finalize_result(retry_error)
                    decision = _finalization_failure_decision(task, retry_error)
            else:
                finalize_result = _failed_finalize_result(error)
                decision = _finalization_failure_decision(task, error)
        write_finalization_result(bundle, finalize_result)
        if finalize_result.error is not None:
            write_review_decision(bundle, decision)

    return TaskRunResult(
        run_id=run_id,
        worktree_path=worktree_path,
        bundle_path=bundle.bundle_path,
        decision=decision,
        finalize=finalize_result,
    )


def executor_config_for_task(config: ProjectConfig, task: TaskContract) -> ExecutorConfig:
    return executor_configs_for_task(config, task)[0]


def executor_configs_for_task(config: ProjectConfig, task: TaskContract) -> list[ExecutorConfig]:
    role = config.model_routing.budget_class_roles.get(task.budget_class)
    if role is None:
        role = config.model_routing.task_type_roles.get(task.task_type)
    if role is None:
        role = config.model_routing.default_role

    if role in config.model_roles:
        primary = config.model_roles[role]
    else:
        primary = config.executor

    configs = [primary]
    for model in primary.fallback_models:
        configs.append(primary.model_copy(update={"model": model, "fallback_models": []}))
    return configs


def _executor_for_config(
    executor_config: ExecutorConfig,
    *,
    stream_callback: Callable[[str, str], None] | None = None,
) -> ExecutorProtocol:
    if executor_config.type != "codex_cli":
        raise ValueError(f"unsupported executor type: {executor_config.type}")
    return CodexExecutor(executor_config, stream_callback=stream_callback)


def _verification_commands(config: ProjectConfig, task: TaskContract) -> list[str]:
    if task.verification.commands:
        return task.verification.commands
    assert task.verification.profile is not None
    try:
        return config.verification_profiles[task.verification.profile].commands
    except KeyError as error:
        raise ValueError(f"verification profile not found: {task.verification.profile}") from error


def _review_line_count(diff: str) -> int:
    return sum(1 for line in diff.splitlines() if line.startswith(("+", "-")))


def _run_executor_attempts(
    *,
    task_id: str,
    executor_configs: list[ExecutorConfig],
    executor: ExecutorProtocol | None,
    max_attempts: int,
    prompt_path: Path,
    worktree_path: Path,
    scratch_dir: Path,
    progress: Callable[[str], None] | None,
) -> ExecutorResult:
    attempts: list[ExecutorAttempt] = []
    last_result: ExecutorResult | None = None
    attempt_configs = _attempt_executor_configs(executor_configs, max_attempts)

    for attempt_number, executor_config in enumerate(attempt_configs, start=1):
        output_dir = scratch_dir / f"executor_attempt_{attempt_number}"
        selected_executor = executor or _executor_for_config(
            executor_config,
            stream_callback=_executor_stream_callback(
                task_id=task_id,
                attempt_number=attempt_number,
                progress=progress,
            ),
        )
        _report(
            progress,
            f"event=executor_attempt_started task={task_id} attempt={attempt_number} "
            f"total={len(attempt_configs)} backend={executor_config.type} model={executor_config.model}",
        )
        result = selected_executor.run(
            prompt_path=prompt_path,
            worktree_path=worktree_path,
            output_dir=output_dir,
        )
        result = _normalize_executor_metadata(result, prompt_path)
        attempts.append(_executor_attempt(attempt_number, result))
        last_result = result
        _report(progress, f"event=executor_attempt_finished task={task_id} attempt={attempt_number} exit_code={result.exit_code}")
        if result.exit_code == 0:
            return result.model_copy(update={"attempts": attempts})

    assert last_result is not None
    return last_result.model_copy(update={"attempts": attempts})


def _attempt_executor_configs(
    executor_configs: list[ExecutorConfig],
    max_attempts: int,
) -> list[ExecutorConfig]:
    if max_attempts <= len(executor_configs):
        return executor_configs[:max_attempts]
    repeated = list(executor_configs)
    while len(repeated) < max_attempts:
        repeated.append(executor_configs[-1])
    return repeated


def _executor_attempt(attempt_number: int, result: ExecutorResult) -> ExecutorAttempt:
    return ExecutorAttempt(
        attempt=attempt_number,
        backend=result.backend,
        model=result.model,
        command=result.command,
        exit_code=result.exit_code,
        stdout_path=result.stdout_path,
        stderr_path=result.stderr_path,
        duration_seconds=result.duration_seconds,
        timed_out=result.timed_out,
        prompt_chars=result.prompt_chars,
        stdout_chars=result.stdout_chars,
        stderr_chars=result.stderr_chars,
    )


def _diagnose_failure(
    *,
    bundle: EvidenceBundle,
    task: TaskContract,
    executor_result: ExecutorResult,
    verification_results: list[CommandResult],
    changed_files: list[str],
    verification_log_path: Path,
    backend: FailureDiagnosisBackend,
    progress: Callable[[str], None] | None,
) -> EvidenceBundle:
    diagnosis_result = backend.diagnose(
        FailureDiagnosisRequest(
            task=task,
            executor_result=executor_result,
            verification_results=verification_results,
            changed_files=changed_files,
            verification_log_path=verification_log_path,
        )
    )
    _report(progress, f"event=failure_diagnosis task={task.task_id} category={diagnosis_result.diagnosis.category}")
    return write_failure_diagnosis(
        bundle,
        _failure_diagnosis_payload(diagnosis_result.diagnosis),
    )


def _failure_diagnosis_payload(diagnosis: FailureDiagnosis) -> dict:
    payload = diagnosis.model_dump(mode="json")
    payload["final_exit_code"] = diagnosis.source_metadata.exit_code
    payload["attempts"] = [
        attempt.model_dump(mode="json") for attempt in diagnosis.source_metadata.attempts
    ]
    return payload


def _attempt_conflict_repair(
    *,
    error: GitFinalizeError,
    task: TaskContract,
    executor_configs: list[ExecutorConfig],
    executor: ExecutorProtocol | None,
    worktree_path: Path,
    scratch_dir: Path,
    verification_commands: list[str],
    verification_timeout_seconds: int,
    progress: Callable[[str], None] | None,
) -> ConflictRepairResult:
    if error.step not in {"rebase", "merge"}:
        return ConflictRepairResult(attempted=False)
    conflicted = conflicted_files(worktree_path)
    if not conflicted:
        return ConflictRepairResult(attempted=False)
    if any(not _path_allowed(path, task.allowed_files) for path in conflicted):
        return ConflictRepairResult(attempted=False, conflicted_files=conflicted)

    _report(progress, f"event=conflict_repair_started task={task.task_id} files={len(conflicted)}")
    prompt_path = write_conflict_repair_prompt(
        path=scratch_dir / "conflict_repair_prompt.md",
        task=task,
        conflicted=conflicted,
        failure=str(error),
    )
    repair_executor = executor or _executor_for_config(
        executor_configs[-1],
        stream_callback=_executor_stream_callback(
            task_id=task.task_id,
            attempt_number=1,
            progress=progress,
            phase="conflict_repair",
        ),
    )
    repair_output_dir = scratch_dir / "conflict_repair"
    repair_result = repair_executor.run(
        prompt_path=prompt_path,
        worktree_path=worktree_path,
        output_dir=repair_output_dir,
    )
    repair_result = _normalize_executor_metadata(repair_result, prompt_path)
    remaining_conflicts = conflicted_files(worktree_path)
    if repair_result.exit_code != 0 or remaining_conflicts:
        return ConflictRepairResult(
            attempted=True,
            conflicted_files=conflicted,
            prompt_path=prompt_path,
            executor_exit_code=repair_result.exit_code,
            resolved=False,
        )

    verification_results = VerificationRunner(timeout_seconds=verification_timeout_seconds).run(
        commands=verification_commands,
        worktree_path=worktree_path,
        output_dir=scratch_dir / "conflict_repair_verification",
    )
    verification_exit_codes = [result.exit_code for result in verification_results]
    resolved = all(exit_code == 0 for exit_code in verification_exit_codes)
    if resolved and error.step == "rebase":
        try:
            continue_rebase(worktree_path)
        except GitFinalizeError:
            resolved = False
    return ConflictRepairResult(
        attempted=True,
        conflicted_files=conflicted,
        prompt_path=prompt_path,
        executor_exit_code=repair_result.exit_code,
        verification_exit_codes=verification_exit_codes,
        resolved=resolved,
    )


def _path_allowed(path: str, allowed_patterns: list[str]) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(path, pattern) for pattern in allowed_patterns)


def _failed_finalize_result(error: GitFinalizeError) -> FinalizeResult:
    return FinalizeResult(failed_step=error.step, error=str(error))


def _finalization_failure_decision(task: TaskContract, error: GitFinalizeError) -> ReviewDecision:
    return ReviewDecision(
        task_id=task.task_id,
        decision=Decision.ESCALATED,
        reviewer=Reviewer.DETERMINISTIC,
        rationale=f"Accepted task could not be finalized: {error}",
        risks=["Accepted work remains in the task worktree or branch."],
        follow_up_tasks=["Inspect finalization.yaml and conflict_repair.yaml if present."],
    )


def _report(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _executor_stream_callback(
    *,
    task_id: str,
    attempt_number: int,
    progress: Callable[[str], None] | None,
    phase: str = "executor",
) -> Callable[[str, str], None] | None:
    if progress is None:
        return None

    def report(stream_name: str, line: str) -> None:
        _report(
            progress,
            f"agent task={task_id} phase={phase} attempt={attempt_number} stream={stream_name} | {line}",
        )

    return report


def _normalize_executor_metadata(result: ExecutorResult, prompt_path: Path) -> ExecutorResult:
    updates = {}
    if result.prompt_chars == 0 and prompt_path.exists():
        updates["prompt_chars"] = len(prompt_path.read_text(encoding="utf-8"))
    if result.stdout_chars == 0 and result.stdout_path.exists():
        updates["stdout_chars"] = len(result.stdout_path.read_text(encoding="utf-8"))
    if result.stderr_chars == 0 and result.stderr_path.exists():
        updates["stderr_chars"] = len(result.stderr_path.read_text(encoding="utf-8"))
    if not updates:
        return result
    return result.model_copy(update=updates)
