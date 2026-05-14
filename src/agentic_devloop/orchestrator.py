from __future__ import annotations

import json
import re
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
    write_task_soft_gate_decision,
    write_verification_environment_repair_input,
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
    SoftGateDecision,
    SoftGateDecisionOutcome,
    SoftGateSeverity,
    TaskSoftGateDecisionRecord,
    TaskContract,
    TaskRun,
    TaskState,
    VerificationEnvironmentRepairInput,
)
from agentic_devloop.supervisor_decisions import (
    BudgetAcceptanceOutcome,
    SoftBudgetAcceptanceDecision,
    SupervisorDecisionType,
    load_supervisor_decision_artifact,
    write_supervisor_decision_artifact,
)
from agentic_devloop.runtime_supervisor import (
    BacklogStateReference,
    BudgetLedgerPaths,
    EvidenceBundlePaths,
    RawLogPaths,
    RepairDecisionClassification,
    ReleaseEvent,
    ReleaseEventKind,
    ReleaseSummaryReference,
    RuntimeSupervisor,
    RuntimeSupervisorStopReason,
    RuntimeSupervisorInput,
    TuningReportPaths,
)
from agentic_devloop.prompt import write_executor_prompt
from agentic_devloop.review import deterministic_review
from agentic_devloop.scientific import analyze_scientific_changes
from agentic_devloop.environment_repair import decide_verification_environment_repair
from agentic_devloop.models import (
    VerificationEnvironmentRepairAction,
    VerificationEnvironmentRepairActionKind,
    VerificationEnvironmentRepairPolicy,
    VerificationEnvironmentRepairRefusal,
)
from agentic_devloop.supervisor_decisions import (
    EnvironmentRepairDecision,
    EnvironmentRepairOutcome,
    EnvironmentRepairPolicyAction,
)
from agentic_devloop.process import run_process
from agentic_devloop.worktree import create_worktree
from agentic_devloop.yaml_io import load_yaml_model


@dataclass(frozen=True)
class _SoftBudgetDetails:
    budget_name: str
    configured_limit: float
    actual: float
    parsed: bool


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


@dataclass(frozen=True)
class _VerificationEnvironmentRepairAttempt:
    failed_command: str
    failed_exit_code: int
    failed_stdout_excerpt: str
    failed_stderr_excerpt: str
    action: VerificationEnvironmentRepairActionKind | None
    outcome: str
    rationale: str
    validators_rerun: tuple[str, ...]
    initial_exit_codes: tuple[int, ...]
    final_exit_codes: tuple[int, ...]
    refusal_reason: str | None = None


MAX_LOG_EXCERPT_CHARS = 4000
_VERIFICATION_ENV_VALUE_ALLOWLIST: set[str] = {"SHARED_RT"}
_WORKTREE_PYTHON = {".venv/bin/python", "./.venv/bin/python"}
_UNSAFE_SHELL_OPERATORS = {"|", "||", "&", "&&", ";", "<", "<<", ">", ">>"}
_ALLOWED_ENV_PREFIX_KEYS = {"PYTHONPATH"}


class VerificationRunner:
    def __init__(self, *, timeout_seconds: int = 600) -> None:
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        *,
        commands: list[str],
        worktree_path: Path,
        output_dir: Path,
        runtime_python_path: str | None = None,
        runtime_env: dict[str, str] | None = None,
        stop_on_failure: bool = True,
    ) -> list[CommandResult]:
        output_dir.mkdir(parents=True, exist_ok=True)
        results: list[CommandResult] = []
        log_lines: list[str] = []

        for index, command in enumerate(commands, start=1):
            resolved_command = rewrite_worktree_local_verification_command(command, safe_runtime=runtime_python_path)
            env_additions = runtime_env or {}
            result = run_process(
                resolved_command,
                cwd=worktree_path,
                timeout_seconds=self.timeout_seconds,
                shell=True,
                env_additions=env_additions,
            )
            stdout_path = output_dir / f"verification_{index}_stdout.log"
            stderr_path = output_dir / f"verification_{index}_stderr.log"
            stdout_path.write_text(result.stdout, encoding="utf-8")
            stderr_path.write_text(result.stderr, encoding="utf-8")
            failure_reason = _failure_reason(result.exit_code, result.timed_out)

            command_result = CommandResult(
                command=resolved_command,
                exit_code=result.exit_code,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                duration_seconds=result.duration_seconds,
                timed_out=result.timed_out,
            )
            results.append(command_result)
            log_lines.append(
                f"[{index}] {resolved_command}\n"
                f"original_command={command}\n"
                f"resolved_command={resolved_command}\n"
                f"cwd={worktree_path}\n"
                f"timeout_seconds={self.timeout_seconds}\n"
                f"env_additions={_render_env_additions(env_additions)}\n"
                f"exit_code={result.exit_code}\n"
                f"timed_out={result.timed_out}\n"
                f"failure_reason={failure_reason}\n"
                f"duration_seconds={result.duration_seconds:.3f}\n"
                f"stdout_path={stdout_path}\n"
                f"stderr_path={stderr_path}\n"
                f"stdout_excerpt:\n{_excerpt(result.stdout)}\n"
                f"stderr_excerpt:\n{_excerpt(result.stderr)}\n"
            )

            if stop_on_failure and result.exit_code != 0:
                break

        (output_dir / "verification.log").write_text("\n".join(log_lines), encoding="utf-8")
        return results


def _excerpt(text: str) -> str:
    if not text:
        return "<empty>"
    if len(text) <= MAX_LOG_EXCERPT_CHARS:
        return text.rstrip("\n")
    omitted = len(text) - MAX_LOG_EXCERPT_CHARS
    return text[:MAX_LOG_EXCERPT_CHARS].rstrip("\n") + f"\n... <truncated {omitted} chars>"


def bounded_verification_excerpt(text: str) -> str:
    return _excerpt(text)


def rewrite_worktree_local_verification_command(command: str, *, safe_runtime: str | None) -> str:
    if not safe_runtime:
        return command
    if ".venv/bin/python" not in command:
        return command
    if not is_safe_worktree_python_rewrite_command(command):
        return command
    import shlex

    tokens = shlex.split(command)
    rewritten = False
    updated_tokens: list[str] = []
    for token in tokens:
        if token in _WORKTREE_PYTHON:
            updated_tokens.append(safe_runtime)
            rewritten = True
            continue
        updated_tokens.append(token)
    if not rewritten:
        return command
    return shlex.join(updated_tokens)


def is_safe_worktree_python_rewrite_command(command: str) -> bool:
    if "$(" in command or "`" in command:
        return False
    import shlex

    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False
    if any(token in _UNSAFE_SHELL_OPERATORS for token in tokens):
        return False
    python_token_index = 0
    while python_token_index < len(tokens) and _is_allowed_env_assignment_token(tokens[python_token_index]):
        python_token_index += 1
    if python_token_index >= len(tokens):
        return False
    return tokens[python_token_index] in _WORKTREE_PYTHON


def _is_allowed_env_assignment_token(token: str) -> bool:
    if "=" not in token:
        return False
    key, value = token.split("=", 1)
    if not key or value == "":
        return False
    if key not in _ALLOWED_ENV_PREFIX_KEYS:
        return False
    return key.replace("_", "").isalnum() and key[0].isalpha()


def _render_env_additions(env_additions: dict[str, str]) -> str:
    if not env_additions:
        return "<none>"
    items: list[str] = []
    for key, value in sorted(env_additions.items()):
        if key in _VERIFICATION_ENV_VALUE_ALLOWLIST:
            items.append(f"{key}={value}")
            continue
        items.append(f"{key}=<redacted>")
    return ", ".join(items)


def _failure_reason(exit_code: int, timed_out: bool) -> str:
    if timed_out:
        return "timeout"
    if exit_code != 0:
        return f"nonzero_exit_{exit_code}"
    return "<none>"


def apply_pythonpath_prefix_repair(*, runtime_env: dict[str, str] | None, pythonpath_prefix: str) -> dict[str, str]:
    if not _is_allowed_env_assignment_token(pythonpath_prefix):
        raise ValueError("pythonpath_prefix must be a safe KEY=value token for an allowed env prefix")
    key, value = pythonpath_prefix.split("=", 1)
    updated = dict(runtime_env or {})
    updated[key] = value
    return updated


def verification_environment_capture_commands(*, safe_runtime: str | None, failed_command: str) -> list[str]:
    import shlex

    python = shlex.quote(safe_runtime) if safe_runtime else "python3"
    sys_path_script = 'import sys; print(sys.executable); print("\\n".join(sys.path))'
    commands: list[str] = [
        f"{python} -V",
        f"{python} -c {shlex.quote(sys_path_script)}",
        f"{python} -m pip --version",
    ]
    failed = failed_command.strip()
    if failed:
        try:
            tokens = shlex.split(failed)
        except ValueError:
            tokens = []
        if tokens:
            candidate = tokens[0]
            if candidate and all(op not in candidate for op in _UNSAFE_SHELL_OPERATORS):
                commands.append(f"command -v {shlex.quote(candidate)}")
    return commands


def make_run_id(release_id: str, task_id: str, now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{release_id}_{task_id}"


def _unique_run_id(
    *,
    base_run_id: str,
    runs_dir: Path,
    worktree_root: Path,
    task_id: str,
) -> str:
    run_id = base_run_id
    suffix = 2
    while (runs_dir / run_id / task_id).exists() or (worktree_root / run_id).exists():
        run_id = f"{base_run_id}_retry{suffix}"
        suffix += 1
    return run_id


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
    run_id = _unique_run_id(
        base_run_id=make_run_id(task.release_id, task.task_id, now),
        runs_dir=runs_dir,
        worktree_root=config.worktree_root,
        task_id=task.task_id,
    )
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
            config=config,
            task=task,
            executor_result=executor_result,
            executor_configs=executor_configs,
            verification_results=[],
            changed_files=current_changed_files,
            verification_log_path=verification_log_path,
            backend=diagnosis_backend,
            max_executor_attempts=config.budget.max_executor_attempts_per_task,
            progress=progress,
            verification_environment_repair_input_path=None,
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
        runtime_python_path=(
            str(config.verification_runtime.python_path) if config.verification_runtime is not None else None
        ),
        runtime_env=(config.verification_runtime.env if config.verification_runtime is not None else None),
    )
    repair_attempt: _VerificationEnvironmentRepairAttempt | None = None
    if any(result.exit_code != 0 for result in verification_results):
        repaired_results, attempt = _maybe_apply_verification_environment_repair(
            config=config,
            task=task,
            worktree_path=worktree_path,
            scratch_dir=scratch_dir,
            verification_commands=verification_commands,
            verification_results=verification_results,
        )
        if attempt is not None:
            repair_attempt = attempt
            verification_results = repaired_results
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
    decision, bundle = _apply_budget_soft_gate_decision(
        decision=decision,
        bundle=bundle,
        release_id=task.release_id,
    )
    bundle = write_scientific_outputs(bundle, task, scientific_review)
    verification_environment_repair_input_path: Path | None = None
    if repair_attempt is not None or any(result.exit_code != 0 for result in verification_results):
        verification_environment_repair_input_path = _capture_verification_environment_repair_input(
            bundle=bundle,
            config=config,
            task=task,
            worktree_path=worktree_path,
            verification_results=verification_results,
            verification_log_path=bundle.verification_log_path,
            prior_repair_attempts=[repair_attempt] if repair_attempt is not None else [],
        )
        if repair_attempt is not None:
            _write_verification_environment_repair_supervisor_decision(
                bundle=bundle,
                task=task,
                attempt=repair_attempt,
                verification_environment_repair_input_path=verification_environment_repair_input_path,
            )
        bundle = _diagnose_failure(
            bundle=bundle,
            config=config,
            task=task,
            executor_result=executor_result,
            executor_configs=executor_configs,
            verification_results=verification_results,
            changed_files=current_changed_files,
            verification_log_path=bundle.verification_log_path,
            backend=diagnosis_backend,
            max_executor_attempts=config.budget.max_executor_attempts_per_task,
            progress=progress,
            verification_environment_repair_input_path=verification_environment_repair_input_path,
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
                executor_configs=conflict_repair_executor_configs(config, executor_configs),
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

    return _executor_configs_for_role(config, role)


def conflict_repair_executor_configs(
    config: ProjectConfig,
    default_executor_configs: list[ExecutorConfig],
) -> list[ExecutorConfig]:
    if "repair" not in config.model_roles:
        return default_executor_configs
    return _executor_configs_for_role(config, "repair")


def _executor_configs_for_role(config: ProjectConfig, role: str) -> list[ExecutorConfig]:
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
    heartbeat_callback: Callable[[float], None] | None = None,
) -> ExecutorProtocol:
    if executor_config.type != "codex_cli":
        raise ValueError(f"unsupported executor type: {executor_config.type}")
    return CodexExecutor(
        executor_config,
        stream_callback=stream_callback,
        heartbeat_callback=heartbeat_callback,
    )


def _apply_budget_soft_gate_decision(
    *,
    decision: ReviewDecision,
    bundle: EvidenceBundle,
    release_id: str,
) -> tuple[ReviewDecision, EvidenceBundle]:
    if not decision.soft_gate_findings:
        return decision, bundle

    severe_threshold = {SoftGateSeverity.HIGH, SoftGateSeverity.CRITICAL}
    severe = any(finding.severity in severe_threshold for finding in decision.soft_gate_findings)
    finding = decision.soft_gate_findings[0].model_copy(
        update={
            "evidence_paths": [
                bundle.changed_files_path,
                bundle.git_diff_path,
                bundle.run_state_path,
                bundle.verification_log_path,
            ]
        }
    )
    budget_details = _soft_budget_details_from_finding(finding.risk, finding_id=finding.finding_id)
    if severe:
        outcome = SoftGateDecisionOutcome.REJECT
        final_decision = Decision.NEEDS_REVISION
        rationale = "Scope-risk budget overage severity is high; task must be split before acceptance."
        fallback_plan = "Split the task scope and rerun verification."
        supervisor_outcome = BudgetAcceptanceOutcome.SPLIT_TASK
    else:
        outcome = SoftGateDecisionOutcome.ACCEPT_WITH_MITIGATION
        final_decision = Decision.ACCEPTED
        rationale = (
            f"Modest scope-risk budget overage accepted because hard invariants and verification passed "
            f"(actual={budget_details.actual:g}, limit={budget_details.configured_limit:g})."
        )
        fallback_plan = "Escalate to task split if overage repeats in the next attempt."
        supervisor_outcome = BudgetAcceptanceOutcome.ACCEPT_OVERAGE
    if not budget_details.parsed:
        rationale = f"{rationale} Raw budget risk was unparseable and recorded for audit: {finding.risk}"

    bundle = write_task_soft_gate_decision(
        bundle,
        TaskSoftGateDecisionRecord(
            task_id=decision.task_id,
            finding=finding,
            decision=SoftGateDecision(
                finding_id=finding.finding_id,
                decision=outcome,
                rationale=rationale,
                fallback_plan=fallback_plan,
                validators_rerun=["verification", "allowed_files", "scientific_review"],
                evidence_paths=[bundle.verification_log_path, bundle.run_state_path],
            ),
        ),
    )
    soft_gate_path = bundle.soft_gate_decision_path
    if soft_gate_path is None:
        raise RuntimeError("soft gate decision artifact was not written")
    artifact_path = write_supervisor_decision_artifact(
        release_bundle_path=bundle.bundle_path,
        decision=SoftBudgetAcceptanceDecision.model_validate(
            {
                "decision_id": f"{decision.task_id}__{finding.finding_id}".replace(":", "_"),
                "release_id": release_id,
                "decided_at": datetime.now(UTC),
                "decided_by": "deterministic_kernel",
                "rationale": rationale,
                "evidence_paths": [
                    Path("changed_files.txt"),
                    Path("git_diff.patch"),
                    Path("run_state.json"),
                    Path("verification.log"),
                    Path("soft_gate_decision.json"),
                ],
                "decision_type": SupervisorDecisionType.SOFT_BUDGET_ACCEPTANCE,
                "budget_name": budget_details.budget_name,
                "configured_limit": budget_details.configured_limit,
                "actual": budget_details.actual,
                "outcome": supervisor_outcome,
            }
        ),
    )
    loaded_decision = load_supervisor_decision_artifact(artifact_path)
    if (
        isinstance(loaded_decision, SoftBudgetAcceptanceDecision)
        and loaded_decision.outcome != BudgetAcceptanceOutcome.ACCEPT_OVERAGE
    ):
        final_decision = Decision.NEEDS_REVISION
    return (
        decision.model_copy(
            update={
                "decision": final_decision,
                "rationale": rationale,
                "reviewer": Reviewer.HYBRID,
            }
        ),
        bundle,
    )


_SOFT_BUDGET_RISK_PATTERN = re.compile(
    r"over budget:\s*(?P<actual>\d+)\s+exceeds\s+(?P<limit>\d+)"
)


def _soft_budget_details_from_finding(risk: str, *, finding_id: str) -> _SoftBudgetDetails:
    budget_name = "soft_budget"
    if finding_id.endswith(":changed_files_budget"):
        budget_name = "max_changed_files_per_task"
    elif finding_id.endswith(":diff_lines_budget"):
        budget_name = "max_diff_lines_per_task"

    match = _SOFT_BUDGET_RISK_PATTERN.search(risk)
    if match is None:
        return _SoftBudgetDetails(
            budget_name=f"{budget_name}_unparsed",
            configured_limit=1.0,
            actual=1.0,
            parsed=False,
        )

    actual = float(match.group("actual"))
    configured_limit = float(match.group("limit"))
    if configured_limit <= 0 or actual < configured_limit:
        return _SoftBudgetDetails(
            budget_name=f"{budget_name}_unparsed",
            configured_limit=1.0,
            actual=1.0,
            parsed=False,
        )

    return _SoftBudgetDetails(
        budget_name=budget_name,
        configured_limit=configured_limit,
        actual=actual,
        parsed=True,
    )


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
            heartbeat_callback=_executor_heartbeat_callback(
                task_id=task_id,
                attempt_number=attempt_number,
                model=executor_config.model,
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
    config: ProjectConfig,
    task: TaskContract,
    executor_result: ExecutorResult,
    executor_configs: list[ExecutorConfig],
    verification_results: list[CommandResult],
    changed_files: list[str],
    verification_log_path: Path,
    backend: FailureDiagnosisBackend,
    max_executor_attempts: int,
    progress: Callable[[str], None] | None,
    verification_environment_repair_input_path: Path | None = None,
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
    runtime_supervisor_payload = _runtime_supervisor_payload(
        config=config,
        task=task,
        executor_result=executor_result,
        executor_configs=executor_configs,
        max_executor_attempts=max_executor_attempts,
        bundle=bundle,
        verification_log_path=verification_log_path,
    )
    return write_failure_diagnosis(
        bundle,
        _failure_diagnosis_payload(
            diagnosis_result.diagnosis,
            runtime_supervisor_payload,
            verification_environment_repair_input_path,
        ),
    )


def _failure_diagnosis_payload(
    diagnosis: FailureDiagnosis,
    runtime_supervisor: dict | None = None,
    verification_environment_repair_input_path: Path | None = None,
) -> dict:
    payload = diagnosis.model_dump(mode="json")
    payload["final_exit_code"] = diagnosis.source_metadata.exit_code
    payload["attempts"] = [
        attempt.model_dump(mode="json") for attempt in diagnosis.source_metadata.attempts
    ]
    if runtime_supervisor is not None:
        payload["runtime_supervisor"] = runtime_supervisor
    if verification_environment_repair_input_path is not None:
        payload["verification_environment_repair_input_path"] = str(verification_environment_repair_input_path)
    return payload


def _capture_verification_environment_repair_input(
    *,
    bundle: EvidenceBundle,
    config: ProjectConfig,
    task: TaskContract,
    worktree_path: Path,
    verification_results: list[CommandResult],
    verification_log_path: Path,
    prior_repair_attempts: list[_VerificationEnvironmentRepairAttempt],
) -> Path | None:
    failed = next((result for result in verification_results if result.exit_code != 0), None)
    stdout_excerpt = _read_command_output_excerpt(failed.stdout_path) if failed is not None else "<empty>"
    stderr_excerpt = _read_command_output_excerpt(failed.stderr_path) if failed is not None else "<empty>"
    failed_command = failed.command if failed is not None else (prior_repair_attempts[0].failed_command if prior_repair_attempts else "<unknown>")
    failed_exit_code = failed.exit_code if failed is not None else (prior_repair_attempts[0].failed_exit_code if prior_repair_attempts else None)
    if prior_repair_attempts:
        failed_command = prior_repair_attempts[0].failed_command
        failed_exit_code = prior_repair_attempts[0].failed_exit_code
        stdout_excerpt = prior_repair_attempts[0].failed_stdout_excerpt
        stderr_excerpt = prior_repair_attempts[0].failed_stderr_excerpt
    repair_input = VerificationEnvironmentRepairInput(
        command=failed_command,
        exit_code=failed_exit_code,
        stdout_excerpt=stdout_excerpt,
        stderr_excerpt=stderr_excerpt,
        allowed_files_snapshot=task.allowed_files,
    )
    payload = {
        "repair_input": repair_input.model_dump(mode="json"),
        "resolved_command": failed_command,
        "verification_runtime_python_path": (
            str(config.verification_runtime.python_path) if config.verification_runtime is not None else None
        ),
        "verification_runtime_env_keys": (
            sorted(config.verification_runtime.env.keys()) if config.verification_runtime is not None else []
        ),
        "worktree_path": str(worktree_path),
        "project_id": config.project_id,
        "verification_log_path": str(verification_log_path),
        "stdout_path": str(failed.stdout_path) if failed is not None and failed.stdout_path is not None else None,
        "stderr_path": str(failed.stderr_path) if failed is not None and failed.stderr_path is not None else None,
        "prior_repair_attempts": [
            {
                "action": str(attempt.action) if attempt.action is not None else None,
                "outcome": attempt.outcome,
                "rationale": attempt.rationale,
                "validators_rerun": list(attempt.validators_rerun),
                "initial_exit_codes": list(attempt.initial_exit_codes),
                "final_exit_codes": list(attempt.final_exit_codes),
            }
            for attempt in prior_repair_attempts
        ],
    }
    return write_verification_environment_repair_input(bundle, payload)


def _read_command_output_excerpt(path: Path | None) -> str:
    if path is None or not path.exists():
        return "<empty>"
    return bounded_verification_excerpt(path.read_text(encoding="utf-8"))


def _maybe_apply_verification_environment_repair(
    *,
    config: ProjectConfig,
    task: TaskContract,
    worktree_path: Path,
    scratch_dir: Path,
    verification_commands: list[str],
    verification_results: list[CommandResult],
) -> tuple[list[CommandResult], _VerificationEnvironmentRepairAttempt | None]:
    failed = next((result for result in verification_results if result.exit_code != 0), None)
    if failed is None:
        return verification_results, None

    stdout_excerpt = _read_command_output_excerpt(failed.stdout_path)
    stderr_excerpt = _read_command_output_excerpt(failed.stderr_path)
    repair_input = VerificationEnvironmentRepairInput(
        command=failed.command,
        exit_code=failed.exit_code,
        stdout_excerpt=stdout_excerpt,
        stderr_excerpt=stderr_excerpt,
        allowed_files_snapshot=task.allowed_files,
    )
    pythonpath_prefix = None
    if (config.repo_path / "src").exists():
        pythonpath_prefix = "PYTHONPATH=src"
    policy = VerificationEnvironmentRepairPolicy(
        allowed_actions=[
            VerificationEnvironmentRepairActionKind.SET_PYTHONPATH_PREFIX,
            VerificationEnvironmentRepairActionKind.CAPTURE_ENVIRONMENT,
        ],
        allowed_files_snapshot=tuple(task.allowed_files),
        pythonpath_prefix=pythonpath_prefix,
    )
    decision = decide_verification_environment_repair(repair_input=repair_input, policy=policy)
    if isinstance(decision, VerificationEnvironmentRepairRefusal):
        if decision.reason.value == "unclassified_failure":
            return verification_results, None
        attempt = _VerificationEnvironmentRepairAttempt(
            failed_command=failed.command,
            failed_exit_code=failed.exit_code,
            failed_stdout_excerpt=stdout_excerpt,
            failed_stderr_excerpt=stderr_excerpt,
            action=None,
            outcome="refused",
            rationale=decision.rationale,
            refusal_reason=str(decision.reason),
            validators_rerun=tuple(verification_commands),
            initial_exit_codes=tuple(result.exit_code for result in verification_results),
            final_exit_codes=tuple(result.exit_code for result in verification_results),
        )
        return verification_results, attempt

    runtime_python_path = str(config.verification_runtime.python_path) if config.verification_runtime is not None else None
    runtime_env = dict(config.verification_runtime.env) if config.verification_runtime is not None else {}
    capture_commands: list[str] = []
    if decision.action == VerificationEnvironmentRepairActionKind.SET_PYTHONPATH_PREFIX and policy.pythonpath_prefix:
        runtime_env = apply_pythonpath_prefix_repair(runtime_env=runtime_env, pythonpath_prefix=policy.pythonpath_prefix)
    elif decision.action == VerificationEnvironmentRepairActionKind.CAPTURE_ENVIRONMENT:
        capture_commands = verification_environment_capture_commands(
            safe_runtime=runtime_python_path,
            failed_command=failed.command,
        )
        capture_dir = scratch_dir / "verification_environment_capture"
        VerificationRunner(timeout_seconds=120).run(
            commands=capture_commands,
            worktree_path=worktree_path,
            output_dir=capture_dir,
            runtime_python_path=runtime_python_path,
            runtime_env=runtime_env,
            stop_on_failure=False,
        )

    prior_log_path = scratch_dir / "verification.log"
    prior_log = prior_log_path.read_text(encoding="utf-8") if prior_log_path.exists() else ""
    rerun_dir = scratch_dir / "verification_repair_rerun"
    rerun_results = VerificationRunner(timeout_seconds=600).run(
        commands=verification_commands,
        worktree_path=worktree_path,
        output_dir=rerun_dir,
        runtime_python_path=runtime_python_path,
        runtime_env=runtime_env,
    )
    rerun_log_path = rerun_dir / "verification.log"
    rerun_log = rerun_log_path.read_text(encoding="utf-8") if rerun_log_path.exists() else ""
    combined_log = "\n".join(
        [
            "# verification_attempt=1",
            prior_log.rstrip("\n"),
            "",
            "# verification_attempt=2",
            rerun_log.rstrip("\n"),
            "",
        ]
    )
    prior_log_path.write_text(combined_log, encoding="utf-8")

    attempt = _VerificationEnvironmentRepairAttempt(
        failed_command=failed.command,
        failed_exit_code=failed.exit_code,
        failed_stdout_excerpt=stdout_excerpt,
        failed_stderr_excerpt=stderr_excerpt,
        action=decision.action,
        outcome="applied_and_rerun",
        rationale=decision.rationale,
        refusal_reason=None,
        validators_rerun=tuple(verification_commands),
        initial_exit_codes=tuple(result.exit_code for result in verification_results),
        final_exit_codes=tuple(result.exit_code for result in rerun_results),
    )
    return rerun_results, attempt


def _write_verification_environment_repair_supervisor_decision(
    *,
    bundle: EvidenceBundle,
    task: TaskContract,
    attempt: _VerificationEnvironmentRepairAttempt,
    verification_environment_repair_input_path: Path | None,
) -> None:
    evidence_paths: list[Path] = [
        Path("contract.yaml"),
        Path("run_state.json"),
        Path("verification.log"),
        Path("git_diff.patch"),
        Path("changed_files.txt"),
    ]
    if verification_environment_repair_input_path is not None:
        evidence_paths.append(Path(verification_environment_repair_input_path.name))

    selected_policy_action = (
        EnvironmentRepairPolicyAction.APPLY_REPAIR_AND_RETRY
        if attempt.action == VerificationEnvironmentRepairActionKind.SET_PYTHONPATH_PREFIX
        else EnvironmentRepairPolicyAction.CAPTURE_EVIDENCE_ONLY
        if attempt.action == VerificationEnvironmentRepairActionKind.CAPTURE_ENVIRONMENT
        else EnvironmentRepairPolicyAction.STOP
    )
    refusal_reason = None
    if selected_policy_action == EnvironmentRepairPolicyAction.STOP:
        refusal_reason = (
            f"verification_environment_repair_refused reason={attempt.refusal_reason or 'unknown'} "
            f"rationale={attempt.rationale}"
        )

    decision = EnvironmentRepairDecision.model_validate(
        {
            "decision_id": f"{task.task_id}__verification_environment_repair",
            "release_id": task.release_id,
            "decided_at": datetime.now(UTC),
            "decided_by": "deterministic_kernel",
            "rationale": attempt.rationale,
            "evidence_paths": evidence_paths,
            "policy_basis": "deterministic_verification_environment_repair_policy_v1",
            "selected_policy_action": selected_policy_action,
            "outcome": (
                EnvironmentRepairOutcome.APPLY_AND_RETRY
                if selected_policy_action == EnvironmentRepairPolicyAction.APPLY_REPAIR_AND_RETRY
                else EnvironmentRepairOutcome.CAPTURE_ONLY
                if selected_policy_action == EnvironmentRepairPolicyAction.CAPTURE_EVIDENCE_ONLY
                else EnvironmentRepairOutcome.STOP
            ),
            "fallback_plan": "Escalate to operator if verification remains failing after bounded environment repair attempt.",
            "source_evidence_paths": [Path("verification.log")],
            "retry_budget_impact": "decrement_retry_budget_by_1",
            "validators_to_rerun": list(attempt.validators_rerun),
            "refusal_reason": refusal_reason,
            "capture_commands": [],
        }
    )
    write_supervisor_decision_artifact(release_bundle_path=bundle.bundle_path, decision=decision)


def _runtime_supervisor_payload(
    *,
    config: ProjectConfig,
    task: TaskContract,
    executor_result: ExecutorResult,
    executor_configs: list[ExecutorConfig],
    max_executor_attempts: int,
    bundle: EvidenceBundle,
    verification_log_path: Path,
) -> dict | None:
    attempts_used = len(executor_result.attempts) or 1
    exhausted_attempts = executor_result.exit_code != 0 and attempts_used >= max_executor_attempts
    if not executor_result.timed_out and not exhausted_attempts:
        return None

    classification = (
        RepairDecisionClassification.LONG_RUNNING_WORKER_ACTIVE
        if executor_result.timed_out
        else RepairDecisionClassification.MODEL_CAPABILITY_MISMATCH
    )
    supervisor = RuntimeSupervisor()
    supervisor_input = RuntimeSupervisorInput(
        classification=classification,
        attempt=attempts_used,
        max_retries=max_executor_attempts,
        release_event=ReleaseEvent(
            kind=ReleaseEventKind.TASK_FAILED,
            message=f"task={task.task_id} executor_failed={executor_result.exit_code != 0} timed_out={executor_result.timed_out}",
            event_path=bundle.bundle_path / "event.jsonl",
        ),
        release_summary=ReleaseSummaryReference(
            release_id=task.release_id,
            summary_path=bundle.bundle_path / "release_summary.yaml",
        ),
        evidence_bundle_paths=EvidenceBundlePaths(
            bundle_path=bundle.bundle_path,
            changed_files_path=bundle.changed_files_path,
            verification_log_path=verification_log_path,
        ),
        raw_log_paths=RawLogPaths(
            supervisor_log_path=bundle.bundle_path / "supervisor.log",
            worker_stdout_path=executor_result.stdout_path,
            worker_stderr_path=executor_result.stderr_path,
        ),
        budget_ledger_paths=BudgetLedgerPaths(
            repair_budget_ledger_path=bundle.bundle_path / "repair_budget.yaml",
            retry_budget_ledger_path=bundle.bundle_path / "retry_budget.yaml",
        ),
        tuning_report_paths=TuningReportPaths(
            model_tuning_report_path=bundle.bundle_path / "model_tuning.yaml",
            verification_tuning_report_path=bundle.bundle_path / "verification_tuning.yaml",
        ),
        backlog_state_reference=BacklogStateReference(
            backlog_state_path=(config.repo_state_path or Path("repo_state") / config.project_id) / "backlog_state.yaml",
            active_epic_id=task.release_id,
        ),
    )
    decision = supervisor.decide(supervisor_input)
    payload: dict = {
        "classification": str(classification),
        "decision": str(decision.decision),
        "attempt": decision.attempt,
        "max_retries": decision.max_retries,
        "remaining_retries": decision.remaining_retries,
        "source_evidence_paths": [str(path) for path in supervisor_input.source_evidence_paths],
    }
    if decision.stop_reason is not None:
        payload["stop_reason"] = str(decision.stop_reason)

    if classification == RepairDecisionClassification.LONG_RUNNING_WORKER_ACTIVE:
        inspection = supervisor.apply_long_running_worker_inspection(
            source_evidence_paths=supervisor_input.source_evidence_paths,
            summary=f"Executor timed out after {executor_result.duration_seconds:.2f}s on attempt {attempts_used}/{max_executor_attempts}.",
            active=True,
        )
        payload["inspection"] = {
            "applied": inspection.applied,
            "action_kind": str(inspection.action_kind),
        }
        if inspection.proposal is not None:
            payload["inspection"]["summary"] = inspection.proposal.summary
            payload["inspection"]["active"] = inspection.proposal.active
        return payload

    escalation_role = config.model_routing.escalation_role
    if escalation_role is None:
        payload["stop_reason"] = str(RuntimeSupervisorStopReason.EXHAUSTED_RETRY_BUDGET)
        payload["model_escalation"] = {
            "applied": False,
            "stop_kind": "bypasses_hard_gate",
            "reason": "No escalation role configured in model_routing.",
        }
        return payload
    routing_configs = _executor_configs_for_role(config, escalation_role)
    available_models = [item.model for item in routing_configs]
    current_model = executor_result.model or executor_configs[-1].model
    recommended_model = next((model for model in available_models if model != current_model), available_models[0])
    escalation = supervisor.apply_model_escalation_recommendation(
        source_evidence_paths=supervisor_input.source_evidence_paths,
        current_model=current_model,
        recommended_model=recommended_model,
        reason="Task execution exhausted supported attempts.",
        retry_budget_remaining=decision.remaining_retries,
        available_models=available_models,
    )
    payload["model_escalation"] = {
        "applied": escalation.applied,
        "action_kind": str(escalation.action_kind),
        "available_models": available_models,
    }
    if escalation.proposal is not None:
        payload["model_escalation"]["current_model"] = escalation.proposal.current_model
        payload["model_escalation"]["recommended_model"] = escalation.proposal.recommended_model
        payload["model_escalation"]["reason"] = escalation.proposal.reason
    if escalation.stop_evidence is not None:
        payload["model_escalation"]["stop_kind"] = str(escalation.stop_evidence.kind)
        payload["model_escalation"]["stop_reason"] = escalation.stop_evidence.reason
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
        heartbeat_callback=_executor_heartbeat_callback(
            task_id=task.task_id,
            attempt_number=1,
            model=executor_configs[-1].model,
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
        runtime_python_path=(
            str(config.verification_runtime.python_path) if config.verification_runtime is not None else None
        ),
        runtime_env=(config.verification_runtime.env if config.verification_runtime is not None else None),
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


def _executor_heartbeat_callback(
    *,
    task_id: str,
    attempt_number: int,
    model: str,
    progress: Callable[[str], None] | None,
    phase: str = "executor",
) -> Callable[[float], None] | None:
    if progress is None:
        return None

    def report(elapsed_seconds: float) -> None:
        _report(
            progress,
            f"event=executor_heartbeat task={task_id} phase={phase} "
            f"attempt={attempt_number} model={model} elapsed_seconds={int(elapsed_seconds)}",
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
