from __future__ import annotations

import json
import os
import shlex
import threading
import re
import warnings
from hashlib import sha256
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Literal

from pydantic import Field, field_validator, model_validator

from agentic_devloop.artifacts import cleanup_task_artifacts
from agentic_devloop.budget import build_budget_ledger, build_tuning_report
from agentic_devloop.config import load_project_config
from agentic_devloop.cost_runtime_governance import build_cost_runtime_governance_decision
from agentic_devloop.evidence import (
    write_final_integration_verification_evidence,
    write_feature_review_decision,
    write_feature_review_recheck,
    write_release_soft_gate_decisions,
)
from agentic_devloop.feature_review import (
    FeatureReviewClassificationError,
    FeatureReviewContextError,
    assemble_feature_review_context,
    classify_feature_review_findings_for_convergence,
    generate_repair_contracts_for_required_findings,
    invoke_feature_reviewer,
    render_feature_review_prompt,
    render_feature_review_prompt_bundle,
)
from agentic_devloop.git_finalize import (
    FinalizeResult,
    GitFinalizeError,
    ensure_branch_from_base,
    merge_integration_branch_to_base,
    push_branch,
)
from agentic_devloop.git_state import git_text
from fnmatch import fnmatch

from agentic_devloop.models import (
    CommandResult,
    Decision,
    FinalReviewContinuationDecision,
    FinalReviewContinuationOutcome,
    FeatureReviewDecision,
    FeatureReviewRecommendation,
    FeatureReviewRecheckRecord,
    FinalIntegrationVerificationEvidence,
    OverlapFinding,
    ReleaseOverlapReport,
    ReleasePlan,
    ReleaseFinalizationPolicy,
    ReleaseFinalizationPolicyName,
    ReleaseSoftGateDecisionRecord,
    ReviewDecision,
    SoftGateDecision,
    SoftGateDecisionOutcome,
    SoftGateFinding,
    SoftGateSeverity,
    StrictModel,
    TaskSoftGateDecisionRecord,
    TaskContract,
)
from agentic_devloop.process import run_process
from agentic_devloop.orchestrator import ExecutorProtocol, TaskRunResult, branch_name, run_task
from agentic_devloop.runtime_supervisor import (
    BacklogStateReference,
    BudgetLedgerPaths,
    EvidenceBundlePaths,
    PlannerAdmissionRepairDecisionArtifact,
    RawLogPaths,
    ReleaseEvent,
    ReleaseEventKind,
    ReleaseSummaryReference,
    RepairDecisionClassification,
    RepairActionKind,
    RuntimeSupervisor,
    RuntimeSupervisorDecisionKind,
    RuntimeSupervisorStopReason,
    RuntimeSupervisorInput,
    TuningReportPaths,
    load_planner_admission_repair_decision_artifact,
)
from agentic_devloop.supervisor_decisions import (
    CostRuntimeGovernanceAction,
    CostRuntimeGovernanceDecision,
    DecisionRiskLevel,
    EnvironmentRepairDecision,
    EnvironmentRepairOutcome,
    FeatureReviewFindingAction,
    FeatureReviewFindingClassification,
    FeatureReviewFindingClassificationDecision,
    FeatureReviewFindingOutcome,
    FinalReviewFindingAdjudicationAction,
    FinalReviewFindingAdjudicationClassification,
    FinalReviewFindingAdjudicationDecision,
    FinalReviewFindingAdjudicationOutcome,
    ModelOutputNormalizationAction,
    ModelOutputNormalizationDecision,
    ModelOutputNormalizationOutcome,
    ModelOutputValidationError,
    ReleaseSchedulingAction,
    ReleaseSchedulingDecision,
    ReleaseSchedulingStalenessInputs,
    SchedulingOutcome,
    ScopeRiskAction,
    ScopeRiskAffectedScope,
    ScopeRiskBudgetPolicyDecision,
    ScopeRiskClassification,
    ScopeRiskOutcome,
    SupervisorDecisionType,
    load_supervisor_decision_artifact,
    supervisor_decision_artifact_path,
    write_supervisor_decision_artifact,
)
from agentic_devloop.state_review import (
    collect_state_review_snapshot,
    write_state_review_context_bundle,
    write_state_review_snapshot_artifact,
)
from agentic_devloop.state_store import FinalReviewFollowUpMemoryReference, StateStore
from agentic_devloop.yaml_io import load_yaml_model


_LEGACY_SUPERVISOR_DECISION_WARNING_PREFIX = (
    "loaded legacy supervisor decision artifact without validators_to_rerun:"
)


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


def rewrite_worktree_local_verification_command(command: str, *, safe_runtime: str | None) -> str:
    if not safe_runtime:
        return command
    if ".venv/bin/python" not in command:
        return command
    if not is_safe_worktree_python_rewrite_command(command):
        return command
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


def _load_supervisor_decision_artifact_silencing_legacy_warning(path: Path) -> tuple[StrictModel, bool]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        loaded = load_supervisor_decision_artifact(path)
    legacy_warning_loaded = False
    for warning in caught:
        if (
            warning.category is UserWarning
            and str(warning.message).startswith(_LEGACY_SUPERVISOR_DECISION_WARNING_PREFIX)
        ):
            legacy_warning_loaded = True
            continue
        warnings.warn(warning.message, warning.category, stacklevel=2)
    return loaded, legacy_warning_loaded


@dataclass(frozen=True)
class ReleaseRunResult:
    release_id: str
    run_id: str
    summary_path: Path
    log_path: Path
    review_path: Path
    metrics_path: Path
    budget_path: Path
    tuning_path: Path
    task_results: list[TaskRunResult]
    decision: Decision
    integration_branch: str | None = None
    finalization: FinalizeResult | None = None
    finalization_gate: dict[str, object] | None = None
    finalization_decision_path: Path | None = None
    feature_review_path: Path | None = None
    feature_review_recheck_path: Path | None = None
    final_review_continuation_decision_path: Path | None = None
    final_integration_verification_path: Path | None = None
    feature_review_prompt_path: Path | None = None
    feature_review_stdout_path: Path | None = None
    feature_review_stderr_path: Path | None = None
    feature_review_metadata_path: Path | None = None
    feature_review_bundle_manifest_paths: list[Path] = field(default_factory=list)
    feature_review_output_normalization_decision_path: Path | None = None
    feature_review_normalized_artifact_path: Path | None = None
    feature_review_proposals: list["FeatureReviewProposalRecord"] = field(default_factory=list)
    scope_risk_budget_policy_decision_paths: list[Path] = field(default_factory=list)
    scope_risk_budget_policy_gate: dict[str, object] | None = None


@dataclass(frozen=True)
class FeatureReviewLoopResult:
    task_results: list[TaskRunResult]
    feature_review_path: Path | None
    feature_review_recheck_path: Path | None
    feature_review_decision: FeatureReviewDecision | None
    feature_review_recheck: FeatureReviewRecheckRecord | None
    feature_review_proposals: list["FeatureReviewProposalRecord"]
    gating_decision: Decision
    final_integration_verification_path: Path | None = None
    final_review_finding_adjudication_paths: list[Path] = field(default_factory=list)
    feature_review_prompt_path: Path | None = None
    feature_review_stdout_path: Path | None = None
    feature_review_stderr_path: Path | None = None
    feature_review_metadata_path: Path | None = None
    feature_review_bundle_manifest_paths: list[Path] = field(default_factory=list)
    feature_review_output_normalization_decision_path: Path | None = None
    feature_review_normalized_artifact_path: Path | None = None


_RELEASE_BUDGET_SOFT_OVERAGE_RATIO = 0.2
class ReleaseFinalizationGate(StrictModel):
    allowed: bool
    reason: Literal[
        "allowed",
        "unresolved_required_findings",
        "release_decision_not_accepted",
    ]
    unresolved_required_finding_ids: list[str] = Field(default_factory=list)
    decision: Decision

    @field_validator("unresolved_required_finding_ids")
    @classmethod
    def unresolved_required_finding_ids_must_be_sorted_unique_and_non_empty(
        cls, values: list[str]
    ) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            if not value.strip():
                raise ValueError("unresolved_required_finding_ids must not contain empty strings")
            cleaned.append(value)
        if cleaned != sorted(cleaned):
            raise ValueError("unresolved_required_finding_ids must be sorted")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("unresolved_required_finding_ids must not contain duplicates")
        return cleaned

    @model_validator(mode="after")
    def gate_invariants(self) -> "ReleaseFinalizationGate":
        if self.allowed:
            if self.reason != "allowed":
                raise ValueError("finalization gate allowed=true requires reason='allowed'")
            if self.unresolved_required_finding_ids:
                raise ValueError("finalization gate allowed=true requires no unresolved required finding ids")
            if self.decision != Decision.ACCEPTED:
                raise ValueError("finalization gate allowed=true requires decision='accepted'")
            return self

        if self.reason == "unresolved_required_findings" and not self.unresolved_required_finding_ids:
            raise ValueError(
                "finalization gate reason='unresolved_required_findings' requires unresolved_required_finding_ids"
            )
        if self.reason == "release_decision_not_accepted" and self.decision == Decision.ACCEPTED:
            raise ValueError(
                "finalization gate reason='release_decision_not_accepted' requires decision != 'accepted'"
            )
        return self


class FeatureReviewProposalRecord(StrictModel):
    finding_id: str
    classification: Literal["scope_expansion", "backlog_follow_up"]
    selected_action: Literal["defer"]
    decision_artifact_path: str
    matched_previous_finding_id: str | None = None
    attempt: int = Field(ge=1)

    @field_validator("finding_id", "decision_artifact_path")
    @classmethod
    def required_strings_must_be_non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value must be non-empty")
        return cleaned


def make_release_run_id(release_id: str, now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{release_id}_release"


def feature_branch_name(release_id: str) -> str:
    return f"feature/{release_id}"


def _scope_risk_task_ids_from_soft_gate_findings(task_results: list[TaskRunResult]) -> list[str]:
    task_ids: set[str] = set()
    for result in task_results:
        decision = result.decision
        for finding in getattr(decision, "soft_gate_findings", []) or []:
            finding_id = str(getattr(finding, "finding_id", "") or "")
            if not finding_id:
                continue
            if finding_id.endswith(":changed_files_budget") or finding_id.endswith(":diff_lines_budget"):
                task_ids.add(decision.task_id)
                break
    return sorted(task_ids)


_SCOPE_RISK_BUDGET_PATTERN = re.compile(r"over budget:\s*(?P<actual>\d+)\s+exceeds\s+(?P<limit>\d+)")


def _parse_scope_risk_budget_from_finding(*, finding_id: str, risk: str) -> tuple[str, int, int] | None:
    if finding_id.endswith(":changed_files_budget"):
        budget_name = "changed_files"
    elif finding_id.endswith(":diff_lines_budget"):
        budget_name = "diff_lines"
    else:
        return None

    match = _SCOPE_RISK_BUDGET_PATTERN.search(risk)
    if match is None:
        return None
    actual = int(match.group("actual"))
    configured = int(match.group("limit"))
    if configured <= 0 or actual < configured:
        return None
    return (budget_name, configured, actual)


def _scope_risk_metrics_from_task_bundle(bundle_path: Path) -> tuple[int, int] | None:
    run_state_path = bundle_path / "run_state.json"
    if not run_state_path.exists():
        return None
    payload = json.loads(run_state_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return None
    changed_files = payload.get("changed_files")
    diff_lines = payload.get("diff_lines")
    if not isinstance(changed_files, list) or not all(isinstance(item, str) for item in changed_files):
        return None
    if not isinstance(diff_lines, int) or diff_lines < 0:
        return None
    changed_files_count = len([item for item in changed_files if str(item).strip()])
    return (changed_files_count, diff_lines)


def _scope_risk_evidence_paths_for_task_result(
    *,
    result: TaskRunResult,
    release_root: Path,
    task_id: str,
) -> list[Path]:
    candidates = [
        result.bundle_path / "changed_files.txt",
        result.bundle_path / "git_diff.patch",
        result.bundle_path / "run_state.json",
        result.bundle_path / "verification.log",
        result.bundle_path / "soft_gate_decision.json",
        result.bundle_path / "contract.yaml",
    ]
    evidence_dir = release_root / "scope_risk_evidence" / task_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    selected: list[Path] = []
    for path in candidates:
        if not path.exists():
            continue
        target = evidence_dir / path.name
        target.write_bytes(path.read_bytes())
        selected.append(target.relative_to(release_root))
    if selected:
        return selected

    fallback = evidence_dir / "evidence_manifest.json"
    fallback.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "bundle_path": str(result.bundle_path),
                "note": "No standard task evidence files were present when the scope-risk decision was generated.",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return [fallback.relative_to(release_root)]


def _ensure_scope_risk_budget_policy_decisions(
    *,
    release_root: Path,
    release_id: str,
    task_results: list[TaskRunResult],
    required_task_ids: list[str],
    default_changed_files_limit: int,
    default_diff_size_limit: int,
    now: datetime | None = None,
) -> list[Path]:
    generated_paths: list[Path] = []
    loaded = _load_scope_risk_budget_policy_decisions(release_root=release_root)
    existing_task_ids = {
        decision.affected_task_id
        for _, decision in loaded
        if decision.affected_scope == ScopeRiskAffectedScope.TASK and decision.affected_task_id
    }

    task_results_by_id = {result.decision.task_id: result for result in task_results}
    decided_at = now or datetime.now(UTC)
    for task_id in required_task_ids:
        if task_id in existing_task_ids:
            continue
        result = task_results_by_id.get(task_id)
        if result is None:
            continue
        findings = getattr(result.decision, "soft_gate_findings", []) or []
        has_changed_files_budget_finding = any(
            str(getattr(finding, "finding_id", "") or "").endswith(":changed_files_budget")
            for finding in findings
        )
        has_diff_budget_finding = any(
            str(getattr(finding, "finding_id", "") or "").endswith(":diff_lines_budget")
            for finding in findings
        )
        parsed_findings = [
            parsed
            for finding in findings
            for parsed in [_parse_scope_risk_budget_from_finding(finding_id=finding.finding_id, risk=finding.risk)]
            if parsed is not None
        ]
        changed_values = [(configured, actual) for budget_name, configured, actual in parsed_findings if budget_name == "changed_files"]
        diff_values = [(configured, actual) for budget_name, configured, actual in parsed_findings if budget_name == "diff_lines"]
        # Prefer configured policy inputs and structured run_state metrics. The
        # risk-string parser remains a compatibility fallback for legacy bundles.
        configured_changed_files_limit = default_changed_files_limit
        configured_diff_size_limit = default_diff_size_limit

        derived_metrics = _scope_risk_metrics_from_task_bundle(result.bundle_path)
        derived_changed_files = derived_metrics[0] if derived_metrics is not None else None
        derived_diff_lines = derived_metrics[1] if derived_metrics is not None else None

        if derived_changed_files is not None:
            actual_changed_files = derived_changed_files
        elif changed_values:
            actual_changed_files = max(value[1] for value in changed_values)
        else:
            raise RuntimeError(
                "unable to derive changed-files budget actual from task bundle; "
                f"missing or invalid run_state.json at {result.bundle_path / 'run_state.json'}"
            )

        if derived_diff_lines is not None:
            actual_diff_size = derived_diff_lines
        elif diff_values:
            actual_diff_size = max(value[1] for value in diff_values)
        else:
            raise RuntimeError(
                "unable to derive diff-lines budget actual from task bundle; "
                f"missing or invalid run_state.json at {result.bundle_path / 'run_state.json'}"
            )

        classification = ScopeRiskClassification.MECHANICAL
        overage_kinds = []
        if has_changed_files_budget_finding:
            overage_kinds.append("changed-files")
        if has_diff_budget_finding:
            overage_kinds.append("diff-size")
        decision = ScopeRiskBudgetPolicyDecision.model_validate(
            {
                "decision_type": SupervisorDecisionType.SCOPE_RISK_BUDGET_POLICY,
                "decision_id": f"{release_id}__scope_risk__{task_id}",
                "release_id": release_id,
                "decided_at": decided_at,
                "decided_by": "deterministic_scope_risk_budget_policy",
                "rationale": (
                    "Generated deterministic scope-risk decision because no supervisor decision artifact "
                    "was present for a task-level "
                    + (", ".join(overage_kinds) or "scope-risk")
                    + " soft overage. This placeholder stays mechanically classified and blocks finalization "
                    "until an explicit supervisor decision accepts, splits, narrows, replans, or stops."
                ),
                "evidence_paths": _scope_risk_evidence_paths_for_task_result(
                    result=result,
                    release_root=release_root,
                    task_id=task_id,
                ),
                "classification": classification,
                "selected_action": ScopeRiskAction.REPLAN,
                "outcome": ScopeRiskOutcome.REPLAN_AND_RETRY,
                "fallback_plan": (
                    "Stop finalization for this run and require explicit supervisor adjudication "
                    "before accepting the scope-risk overage."
                ),
                "validators_to_rerun": ["verification", "release_summary", "release_metrics", "release_budget"],
                "configured_changed_files_limit": configured_changed_files_limit,
                "actual_changed_files": actual_changed_files,
                "configured_diff_size_limit": configured_diff_size_limit,
                "actual_diff_size": actual_diff_size,
                "affected_scope": ScopeRiskAffectedScope.TASK,
                "affected_task_id": task_id,
                "hard_safety_findings": [],
            }
        )
        generated_paths.append(write_supervisor_decision_artifact(release_bundle_path=release_root, decision=decision))
    return generated_paths


def _load_scope_risk_budget_policy_decisions(
    *,
    release_root: Path,
) -> list[tuple[Path, ScopeRiskBudgetPolicyDecision]]:
    supervisor_dir = release_root / "supervisor_decisions"
    if not supervisor_dir.exists():
        return []
    loaded: list[tuple[Path, ScopeRiskBudgetPolicyDecision]] = []
    pattern = f"{SupervisorDecisionType.SCOPE_RISK_BUDGET_POLICY.value}__*.json"
    for candidate in sorted(supervisor_dir.glob(pattern)):
        decision = load_supervisor_decision_artifact(candidate)
        if not isinstance(decision, ScopeRiskBudgetPolicyDecision):
            raise ValueError(
                f"scope risk budget policy decision artifact has unsupported type: {decision.decision_type}"
            )
        loaded.append((candidate, decision))
    return loaded


def _scope_risk_budget_policy_gate(
    *,
    required_task_ids: list[str],
    loaded_decisions: list[tuple[Path, ScopeRiskBudgetPolicyDecision]],
) -> dict[str, object]:
    selected_paths: list[Path] = []
    blocking_paths: list[Path] = []
    blocking_reasons: list[str] = []

    release_scope: list[tuple[Path, ScopeRiskBudgetPolicyDecision]] = [
        item for item in loaded_decisions if item[1].affected_scope == ScopeRiskAffectedScope.RELEASE
    ]
    task_scope: dict[str, list[tuple[Path, ScopeRiskBudgetPolicyDecision]]] = {}
    for path, decision in loaded_decisions:
        if decision.affected_scope != ScopeRiskAffectedScope.TASK:
            continue
        assert decision.affected_task_id is not None
        task_scope.setdefault(decision.affected_task_id, []).append((path, decision))

    def _select_most_recent(
        candidates: list[tuple[Path, ScopeRiskBudgetPolicyDecision]],
    ) -> tuple[Path, ScopeRiskBudgetPolicyDecision]:
        return max(candidates, key=lambda item: (item[1].decided_at, str(item[0])))

    for task_id in required_task_ids:
        decision_pair: tuple[Path, ScopeRiskBudgetPolicyDecision] | None = None
        if task_id in task_scope:
            decision_pair = _select_most_recent(task_scope[task_id])
        elif release_scope:
            decision_pair = _select_most_recent(release_scope)

        if decision_pair is None:
            blocking_reasons.append(f"missing scope-risk budget policy decision for task {task_id}")
            continue

        path, decision = decision_pair
        selected_paths.append(path)
        if decision.outcome != ScopeRiskOutcome.ACCEPTED_WITH_GUARDS:
            blocking_paths.append(path)
            blocking_reasons.append(
                f"scope-risk budget policy decision for task {task_id} requires {decision.outcome.value}"
            )

    gate_allowed = not blocking_reasons
    return {
        "allowed": gate_allowed,
        "reason": "allowed" if gate_allowed else "scope_risk_budget_policy_blocked",
        "required_task_ids": list(required_task_ids),
        "selected_decision_paths": sorted({str(path) for path in selected_paths}),
        "blocking_decision_paths": sorted({str(path) for path in blocking_paths}),
        "blocking_reasons": blocking_reasons,
    }


def _release_scheduling_decision_path(release_root: Path, release_id: str) -> Path:
    return supervisor_decision_artifact_path(
        release_bundle_path=release_root,
        decision_type=SupervisorDecisionType.RELEASE_SCHEDULING,
        decision_id=f"{release_id}__scheduling",
    )


def _cost_runtime_governance_decision_path(release_root: Path, release_id: str) -> Path:
    return supervisor_decision_artifact_path(
        release_bundle_path=release_root,
        decision_type=SupervisorDecisionType.COST_RUNTIME_GOVERNANCE,
        decision_id=release_id,
    )


def _latest_release_run_dir(
    *,
    runs_dir: Path,
    release_id: str,
    exclude_run_id: str | None = None,
) -> Path | None:
    if not runs_dir.exists():
        return None
    suffix = f"_{release_id}_release"
    candidates = [
        path
        for path in runs_dir.iterdir()
        if path.is_dir() and path.name.endswith(suffix) and path.name != exclude_run_id
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.name)


def _infer_budget_class(config: "ProjectConfig") -> str:
    model = config.executor.model
    entry = config.model_catalog.get(model)
    if entry is not None:
        return entry.budget_class
    return "L"


def _load_or_build_cost_runtime_governance_decision(
    *,
    release_root: Path,
    release_id: str,
    runs_dir: Path,
    current_run_id: str,
    config: "ProjectConfig",
    now: datetime | None,
    progress: Callable[[str], None] | None = None,
) -> CostRuntimeGovernanceDecision:
    decision_path = _cost_runtime_governance_decision_path(release_root, release_id)
    if decision_path.exists():
        loaded = load_supervisor_decision_artifact(decision_path)
        if not isinstance(loaded, CostRuntimeGovernanceDecision):
            raise ValueError(
                f"cost-runtime governance decision artifact has unsupported type: {loaded.decision_type}"
            )
        return loaded

    prior_release_run_dir = _latest_release_run_dir(
        runs_dir=runs_dir,
        release_id=release_id,
        exclude_run_id=current_run_id,
    )
    prior_release_metrics_path = (
        prior_release_run_dir / "release_metrics.json"
        if prior_release_run_dir is not None
        else None
    )
    prior_release_tuning_path = (
        prior_release_run_dir / "release_tuning.md"
        if prior_release_run_dir is not None
        else None
    )
    release_metrics_path = (
        prior_release_metrics_path
        if prior_release_metrics_path is not None and prior_release_metrics_path.exists()
        else None
    )
    release_tuning_path = (
        prior_release_tuning_path
        if prior_release_tuning_path is not None and prior_release_tuning_path.exists()
        else None
    )
    fallback_evidence_path: Path | None = None
    if release_metrics_path is None and release_tuning_path is None:
        fallback_evidence_path = release_root / "cost_runtime_governance_fallback_evidence.json"
        fallback_evidence_path.write_text(
            json.dumps(
                {
                    "release_id": release_id,
                    "current_run_id": current_run_id,
                    "reason": "no prior release_metrics.json or release_tuning.md was available",
                    "selected_default": "decomposed",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    decision = build_cost_runtime_governance_decision(
        decision_id=release_id,
        release_id=release_id,
        decided_by="deterministic",
        budget_class=_infer_budget_class(config),
        release_metrics_path=release_metrics_path,
        release_tuning_path=release_tuning_path,
        fallback_evidence_path=fallback_evidence_path,
        decided_at=now,
    )
    written = write_supervisor_decision_artifact(release_bundle_path=release_root, decision=decision)
    if written != decision_path:
        raise RuntimeError(
            f"cost-runtime governance decision artifact was written to unexpected path: {written}"
        )
    loaded = load_supervisor_decision_artifact(decision_path)
    if not isinstance(loaded, CostRuntimeGovernanceDecision):
        raise ValueError(
            f"cost-runtime governance decision artifact has unsupported type: {loaded.decision_type}"
        )
    _report(
        progress,
        "event=cost_runtime_governance_decision "
        f"action={loaded.selected_action.value} outcome={loaded.outcome.value} path={decision_path}",
    )
    return loaded


def _cost_runtime_governance_feature_review_max_repair_loops_override(
    *,
    decision: CostRuntimeGovernanceDecision,
    default_max_repair_loops: int,
) -> int | None:
    if decision.selected_action != CostRuntimeGovernanceAction.REVIEW_CAPPED:
        return None
    return min(default_max_repair_loops, 1)


def _persist_planner_admission_repairs_from_warnings(
    *,
    release_root: Path,
    release_id: str,
    warnings: list[str],
) -> Path | None:
    records: list[dict[str, object]] = []
    decision_paths: list[Path] = []
    for warning in warnings:
        if warning.startswith("supervisor_admission_repair_decision_path="):
            decision_paths.append(Path(warning.split("=", 1)[1]))
            continue
        if not warning.startswith("planner_contract_normalization="):
            continue
        payload_text = warning.split("=", 1)[1]
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            continue
        decision_path = payload.get("supervisor_admission_repair_decision_path")
        if isinstance(decision_path, str) and decision_path.strip():
            decision_paths.append(Path(decision_path))
            continue
        supervisor_decision = payload.get("supervisor_admission_repair_decision")
        if isinstance(supervisor_decision, dict):
            repair_action = supervisor_decision.get("action_payload")
        else:
            repair_action = payload.get("planner_admission_repair_action")
        if not isinstance(repair_action, dict):
            continue
        failure_inputs = repair_action.get("admission_failure_inputs")
        task_id = "unknown"
        if isinstance(failure_inputs, list) and failure_inputs:
            first = failure_inputs[0]
            if isinstance(first, dict):
                raw_task_id = first.get("task_id")
                if isinstance(raw_task_id, str) and raw_task_id.strip():
                    task_id = raw_task_id
        records.append(
            {
                "release_id": release_id,
                "task_id": task_id,
                "selected_action": repair_action.get("selected_action"),
                "outcome": repair_action.get("outcome"),
                "validators_to_rerun": repair_action.get("validators_to_rerun"),
                "validator_rerun_succeeded": bool(payload.get("validator_rerun_succeeded")),
                "planner_admission_repair_applied": bool(
                    supervisor_decision.get("applied")
                    if isinstance(supervisor_decision, dict)
                    else payload.get("planner_admission_repair_applied")
                ),
                "action_kind": (
                    supervisor_decision.get("action_kind")
                    if isinstance(supervisor_decision, dict)
                    else None
                ),
                "decision_type": (
                    supervisor_decision.get("decision_type")
                    if isinstance(supervisor_decision, dict)
                    else None
                ),
            }
        )
    for decision_path in decision_paths:
        decision = load_planner_admission_repair_decision_artifact(decision_path)
        records.append(_planner_admission_record_from_decision(release_id=release_id, decision=decision))
    if not records:
        return None
    artifact_path = release_root / "runtime_supervisor" / "planner_admission_repairs.json"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(
            {
                "release_id": release_id,
                "recorded_at": datetime.now(UTC).isoformat(),
                "records": records,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return artifact_path


def _planner_admission_record_from_decision(
    *,
    release_id: str,
    decision: PlannerAdmissionRepairDecisionArtifact,
) -> dict[str, object]:
    task_id = "unknown"
    if decision.action_payload.admission_failure_inputs:
        candidate = decision.action_payload.admission_failure_inputs[0].task_id.strip()
        if candidate:
            task_id = candidate
    return {
        "release_id": release_id,
        "task_id": task_id,
        "selected_action": decision.action_payload.selected_action.value,
        "outcome": decision.action_payload.outcome.value,
        "validators_to_rerun": list(decision.validators_to_rerun),
        "validator_rerun_succeeded": True,
        "planner_admission_repair_applied": decision.applied,
        "action_kind": decision.action_kind.value,
        "decision_type": decision.decision_type.value,
    }


def run_release(
    *,
    project_id: str,
    release_id: str,
    contract_paths: list[Path] | None = None,
    config_dir: Path = Path("configs"),
    contracts_dir: Path = Path("contracts"),
    runs_dir: Path = Path("runs"),
    executor: ExecutorProtocol | None = None,
    verification_timeout_seconds: int = 600,
    allow_dirty: bool = False,
    commit_on_accept: bool = False,
    merge_on_accept: bool = False,
    push_on_accept: bool = False,
    release_finalize: str = "none",
    integration_branch: str | None = None,
    stop_on_failure: bool = True,
    execution_mode: str = "sequential",
    debug_keep_artifacts: bool = False,
    now: datetime | None = None,
    progress: Callable[[str], None] | None = None,
    planning_warnings: list[str] | None = None,
) -> ReleaseRunResult:
    if execution_mode not in {"sequential", "parallel"}:
        raise ValueError(f"unsupported execution mode: {execution_mode}")
    if release_finalize not in {"none", "merge-main", "push-feature", "push-main"}:
        raise ValueError(f"unsupported release finalization mode: {release_finalize}")
    config = load_project_config(project_id, config_dir, validate_repo=True)
    _ensure_no_existing_worktrees(config.worktree_root)
    run_id = make_release_run_id(release_id, now)
    release_root = runs_dir / run_id
    release_root.mkdir(parents=True, exist_ok=True)
    log_path = release_root / "release.log"
    raw_log_path = release_root / "release.raw.log"
    progress = _multiplexed_progress(progress, log_path, raw_log_path)
    admission_repair_path = _persist_planner_admission_repairs_from_warnings(
        release_root=release_root,
        release_id=release_id,
        warnings=planning_warnings or [],
    )
    if admission_repair_path is not None:
        _report(progress, f"event=admission_repair_records path={admission_repair_path}")
        records = json.loads(admission_repair_path.read_text(encoding="utf-8")).get("records", [])
        for index, record in enumerate(records, start=1):
            _report(
                progress,
                "event=admission_repair_attempt "
                f"attempt={index} task={record.get('task_id', 'unknown')} "
                f"action={record.get('selected_action', 'unknown')} "
                f"outcome={record.get('outcome', 'unknown')}",
            )
    selected_contracts = _select_contracts(
        release_id=release_id,
        config_repo_path=config.repo_path,
        repo_state_path=config.repo_state_path,
        contracts_dir=contracts_dir,
        contract_paths=contract_paths,
    )
    if not selected_contracts:
        raise ValueError(f"no contracts found for release {release_id}")
    selected_tasks = [load_yaml_model(path, TaskContract) for path in selected_contracts]
    for contract_path, task in zip(selected_contracts, selected_tasks):
        if task.release_id != release_id:
            raise ValueError(
                f"contract {contract_path} belongs to release {task.release_id}, expected {release_id}"
            )
    feature_review_source_tasks = _feature_review_source_contracts(
        release_id=release_id,
        contracts_dir=contracts_dir,
        selected_tasks=selected_tasks,
    )
    _ensure_no_existing_task_branches(config.repo_path, release_id, selected_tasks)
    overlap_report = analyze_contract_overlaps(
        selected_tasks,
        unsafe_overlap_paths=config.unsafe_overlap_paths,
    )
    overlap_report_path = _write_release_overlap_report_artifact(
        release_root=release_root,
        overlap_report=overlap_report,
    )
    if overlap_report.has_blocking_findings:
        details = "; ".join(
            f"{finding.first_task_id}/{finding.second_task_id}: {finding.pattern}"
            for finding in overlap_report.findings
            if finding.severity == "blocking"
        )
        raise ValueError(f"release contracts are unsafe for {execution_mode} execution: {details}")

    _report(progress, f"event=release_started run_id={run_id} release={release_id} tasks={len(selected_contracts)} mode={execution_mode}")
    _report(progress, f"event=release_logs log={log_path} raw_log={raw_log_path}")
    feature_branch = integration_branch or feature_branch_name(release_id)
    ensure_branch_from_base(config.repo_path, feature_branch, config.default_base_branch)
    _report(progress, f"event=integration_branch branch={feature_branch} base={config.default_base_branch}")
    if overlap_report.findings:
        _report(
            progress,
            "event=overlap_risk_report "
            f"count={len(overlap_report.findings)} "
            f"path={overlap_report_path} "
            f"severity={'blocking' if overlap_report.has_blocking_findings else 'soft'}",
        )

    completed_task_ids = _completed_release_task_ids(
        runs_dir=runs_dir,
        release_id=release_id,
        integration_branch=feature_branch,
    )
    task_inputs: list[tuple[Path, TaskContract]] = []
    skipped_completed_task_ids: list[str] = []
    for contract_path, task in zip(selected_contracts, selected_tasks):
        if task.task_id in completed_task_ids:
            skipped_completed_task_ids.append(task.task_id)
            continue
        task_inputs.append((contract_path, task))
    dependencies = _release_dependency_map(
        [task for _, task in task_inputs],
        overlap_report,
        completed_task_ids=completed_task_ids,
    )
    if completed_task_ids:
        _report(
            progress,
            "event=completed_release_dependencies tasks="
            + json.dumps(sorted(completed_task_ids), sort_keys=True),
        )
    if skipped_completed_task_ids:
        _report(
            progress,
            "event=completed_release_tasks_skipped tasks="
            + json.dumps(sorted(skipped_completed_task_ids), sort_keys=True),
        )
    if dependencies:
        _report(progress, "event=execution_dag dependencies=" + json.dumps(dependencies, sort_keys=True))
    scheduling_decision = _load_or_build_release_scheduling_decision(
        release_root=release_root,
        release_id=release_id,
        config=config,
        base_branch=config.default_base_branch,
        execution_mode=execution_mode,
        selected_contracts=selected_contracts,
        selected_tasks=selected_tasks,
        overlap_report=overlap_report,
        overlap_report_path=overlap_report_path,
        dependencies=dependencies,
        progress=progress,
    )
    _report(
        progress,
        "event=scheduling_decision "
        f"action={scheduling_decision.selected_action.value} "
        f"outcome={scheduling_decision.outcome.value} "
        f"path={_release_scheduling_decision_path(release_root, release_id)}",
    )

    cost_runtime_governance_decision = _load_or_build_cost_runtime_governance_decision(
        release_root=release_root,
        release_id=release_id,
        runs_dir=runs_dir,
        current_run_id=run_id,
        config=config,
        now=now,
        progress=progress,
    )
    max_feature_review_repair_loops_override = _cost_runtime_governance_feature_review_max_repair_loops_override(
        decision=cost_runtime_governance_decision,
        default_max_repair_loops=config.feature_review_max_repair_loops,
    )
    if max_feature_review_repair_loops_override is not None:
        _report(
            progress,
            "event=cost_runtime_governance_review_cap "
            f"max_feature_review_repair_loops={max_feature_review_repair_loops_override} "
            f"path={_cost_runtime_governance_decision_path(release_root, release_id)}",
        )
    if scheduling_decision.selected_action == ReleaseSchedulingAction.PARALLEL:
        task_results = _run_release_parallel(
            project_id=project_id,
            config_repo_path=config.repo_path,
            config_dir=config_dir,
            runs_dir=runs_dir,
            task_base_branch=feature_branch,
            task_inputs=task_inputs,
            dependencies=dependencies,
            completed_task_ids=completed_task_ids,
            executor=executor,
            verification_timeout_seconds=verification_timeout_seconds,
            allow_dirty=allow_dirty,
            commit_on_accept=commit_on_accept,
            merge_on_accept=merge_on_accept,
            push_on_accept=push_on_accept,
            stop_on_failure=stop_on_failure,
            debug_keep_artifacts=debug_keep_artifacts,
            progress=progress,
        )
    elif scheduling_decision.selected_action == ReleaseSchedulingAction.SEQUENTIAL:
        ordered_task_inputs = _ordered_release_task_inputs(
            task_inputs=task_inputs,
            dependencies=dependencies,
            completed_task_ids=completed_task_ids,
        )
        task_results = _run_release_sequential(
            project_id=project_id,
            config_repo_path=config.repo_path,
            config_dir=config_dir,
            runs_dir=runs_dir,
            release_run_dir=release_root,
            release_id=release_id,
            run_id=run_id,
            repo_state_path=config.repo_state_path,
            task_base_branch=feature_branch,
            task_inputs=ordered_task_inputs,
            executor=executor,
            verification_timeout_seconds=verification_timeout_seconds,
            allow_dirty=allow_dirty,
            commit_on_accept=commit_on_accept,
            merge_on_accept=merge_on_accept,
            push_on_accept=push_on_accept,
            stop_on_failure=stop_on_failure,
            debug_keep_artifacts=debug_keep_artifacts,
            progress=progress,
        )
    else:
        raise ValueError(
            f"unsupported release scheduling action: {scheduling_decision.selected_action.value}"
        )

    feature_review_path: Path | None = None
    feature_review_recheck_path: Path | None = None
    feature_review_decision: FeatureReviewDecision | None = None
    feature_review_recheck: FeatureReviewRecheckRecord | None = None
    feature_review_proposals: list[FeatureReviewProposalRecord] = []
    feature_review_prompt_path: Path | None = None
    feature_review_stdout_path: Path | None = None
    feature_review_stderr_path: Path | None = None
    feature_review_metadata_path: Path | None = None
    feature_review_bundle_manifest_paths: list[Path] = []
    feature_review_output_normalization_decision_path: Path | None = None
    feature_review_normalized_artifact_path: Path | None = None
    scope_risk_budget_policy_decision_paths: list[Path] = []
    scope_risk_budget_policy_gate: dict[str, object] | None = None
    final_integration_verification_path: Path | None = None
    final_review_finding_adjudication_paths: list[Path] = []

    task_decision = (
        Decision.ACCEPTED
        if not task_results and skipped_completed_task_ids
        else _release_decision([result.decision for result in task_results])
    )
    scope_risk_required_task_ids = _scope_risk_task_ids_from_soft_gate_findings(task_results)
    if scope_risk_required_task_ids:
        try:
            generated_scope_risk_paths = _ensure_scope_risk_budget_policy_decisions(
                release_root=release_root,
                release_id=release_id,
                task_results=task_results,
                required_task_ids=scope_risk_required_task_ids,
                default_changed_files_limit=config.budget.max_changed_files_per_task,
                default_diff_size_limit=config.budget.max_diff_lines_per_task,
                now=now,
            )
            if generated_scope_risk_paths:
                _report(
                    progress,
                    "event=scope_risk_budget_policy_decisions_generated paths="
                    + json.dumps([str(path) for path in generated_scope_risk_paths], sort_keys=True),
                )
            scope_risk_loaded = _load_scope_risk_budget_policy_decisions(release_root=release_root)
            scope_risk_budget_policy_decision_paths = [path for path, _ in scope_risk_loaded]
            scope_risk_budget_policy_gate = _scope_risk_budget_policy_gate(
                required_task_ids=scope_risk_required_task_ids,
                loaded_decisions=scope_risk_loaded,
            )
        except Exception as error:  # noqa: BLE001 - invalid artifacts must remain blocking.
            scope_risk_budget_policy_gate = {
                "allowed": False,
                "reason": "scope_risk_budget_policy_invalid",
                "required_task_ids": scope_risk_required_task_ids,
                "selected_decision_paths": [],
                "blocking_decision_paths": [],
                "blocking_reasons": [f"invalid scope-risk budget policy decision artifacts: {type(error).__name__}: {error}"],
            }
        _report(
            progress,
            "event=scope_risk_budget_policy_gate "
            f"allowed={bool(scope_risk_budget_policy_gate.get('allowed'))} "
            f"reason={scope_risk_budget_policy_gate.get('reason')} "
            "blocking_reasons=" + json.dumps(scope_risk_budget_policy_gate.get("blocking_reasons", []), sort_keys=True),
        )
        if not bool(scope_risk_budget_policy_gate.get("allowed")) and task_decision == Decision.ACCEPTED:
            task_decision = Decision.NEEDS_REVISION
    if task_decision == Decision.ACCEPTED and "reviewer" in config.model_roles:
        feature_review_loop = _run_feature_review_and_repair_loop(
            project_id=project_id,
            config=config,
            release_id=release_id,
            run_id=run_id,
            release_root=release_root,
            runs_dir=runs_dir,
            config_dir=config_dir,
            integration_branch=feature_branch,
            task_results=task_results,
            source_contracts=feature_review_source_tasks,
            executor=executor,
            verification_timeout_seconds=verification_timeout_seconds,
            allow_dirty=allow_dirty,
            commit_on_accept=commit_on_accept,
            merge_on_accept=merge_on_accept,
            push_on_accept=push_on_accept,
            debug_keep_artifacts=debug_keep_artifacts,
            progress=progress,
            max_feature_review_repair_loops_override=max_feature_review_repair_loops_override,
        )
        task_results = feature_review_loop.task_results
        feature_review_path = feature_review_loop.feature_review_path
        feature_review_recheck_path = feature_review_loop.feature_review_recheck_path
        feature_review_decision = feature_review_loop.feature_review_decision
        feature_review_recheck = feature_review_loop.feature_review_recheck
        feature_review_proposals = feature_review_loop.feature_review_proposals
        feature_review_prompt_path = feature_review_loop.feature_review_prompt_path
        feature_review_stdout_path = feature_review_loop.feature_review_stdout_path
        feature_review_stderr_path = feature_review_loop.feature_review_stderr_path
        feature_review_metadata_path = feature_review_loop.feature_review_metadata_path
        feature_review_bundle_manifest_paths = list(feature_review_loop.feature_review_bundle_manifest_paths)
        feature_review_output_normalization_decision_path = (
            feature_review_loop.feature_review_output_normalization_decision_path
        )
        feature_review_normalized_artifact_path = feature_review_loop.feature_review_normalized_artifact_path
        if feature_review_loop.final_integration_verification_path is not None:
            final_integration_verification_path = feature_review_loop.final_integration_verification_path
        final_review_finding_adjudication_paths = list(feature_review_loop.final_review_finding_adjudication_paths)
        task_decision = _release_decision([result.decision for result in task_results])
        if feature_review_loop.gating_decision != Decision.ACCEPTED:
            task_decision = feature_review_loop.gating_decision
    release_metrics = _build_release_metrics(
        run_id=run_id,
        release_id=release_id,
        decision=task_decision,
        task_results=task_results,
        raw_log_path=raw_log_path,
        runs_dir=runs_dir,
    )
    metrics_path = _write_release_metrics(
        runs_dir=runs_dir,
        run_id=run_id,
        metrics=release_metrics,
    )
    budget_ledger = build_budget_ledger(release_metrics=release_metrics, budget=config.budget)
    budget_path = _write_release_budget(runs_dir=runs_dir, run_id=run_id, ledger=budget_ledger)
    tuning_path = _write_release_tuning(
        runs_dir=runs_dir,
        run_id=run_id,
        tuning_report=build_tuning_report(ledger=budget_ledger),
    )
    budget_evaluation = _evaluate_release_budget(budget_ledger)
    budget_violations = budget_evaluation["severe_violations"]
    soft_budget_findings = budget_evaluation["soft_findings"]
    release_metrics["budget_violations"] = budget_violations
    release_metrics["soft_budget_findings"] = soft_budget_findings
    release_soft_gate_decision_path: Path | None = None
    decision = _release_decision_with_budget(
        task_decision,
        severe_budget_violations=budget_violations,
    )
    if task_decision == Decision.ACCEPTED and soft_budget_findings:
        release_soft_gate_decision_path = _write_release_budget_soft_decision(
            runs_dir=runs_dir,
            run_id=run_id,
            release_id=release_id,
            findings=soft_budget_findings,
            progress=progress,
        )
    metrics_path = _write_release_metrics(runs_dir=runs_dir, run_id=run_id, metrics=release_metrics)
    if decision != task_decision:
        release_metrics["decision"] = decision
        metrics_path = _write_release_metrics(runs_dir=runs_dir, run_id=run_id, metrics=release_metrics)
        _report(progress, "event=release_budget_exceeded violations=" + json.dumps(budget_violations, sort_keys=True))
    integration_commit = _git_rev_parse(config.repo_path, feature_branch)
    if not integration_commit.strip():
        raise ValueError(f"failed to resolve integration commit for branch {feature_branch}")
    final_integration_verification_summary: dict[str, object] | None = None
    merged_into_integration = any(
        result.finalize is not None and bool(result.finalize.merged)
        for result in task_results
    )
    if decision == Decision.ACCEPTED and merged_into_integration and final_integration_verification_path is None:
        final_integration_verification_path = _run_final_integration_verification(
            release_id=release_id,
            release_root=release_root,
            repo_path=config.repo_path,
            integration_branch=feature_branch,
            integration_commit=integration_commit,
            commands=list(config.verification_profiles["default"].commands),
            timeout_seconds=verification_timeout_seconds,
            progress=progress,
        )
        final_integration_verification = json.loads(
            final_integration_verification_path.read_text(encoding="utf-8")
        )
        final_integration_verification_summary = {
            "release_id": final_integration_verification.get("release_id"),
            "integration_branch": final_integration_verification.get("integration_branch"),
            "integration_commit": final_integration_verification.get("integration_commit"),
            "verification_log_path": final_integration_verification.get("verification_log_path"),
            "worktree_log_path": final_integration_verification.get("worktree_log_path"),
            "success": bool(final_integration_verification.get("success")),
            "command_results": [
                {
                    "command": result.get("command"),
                    "exit_code": result.get("exit_code"),
                    "stdout_path": result.get("stdout_path"),
                    "stderr_path": result.get("stderr_path"),
                    "duration_seconds": result.get("duration_seconds"),
                    "timed_out": bool(result.get("timed_out")),
                }
                for result in final_integration_verification.get("command_results", [])
                if isinstance(result, dict)
            ],
            "verified_at": final_integration_verification.get("verified_at"),
        }
        if not bool(final_integration_verification.get("success")):
            decision = Decision.FAILED
            release_metrics["decision"] = decision
            metrics_path = _write_release_metrics(runs_dir=runs_dir, run_id=run_id, metrics=release_metrics)
            _report(
                progress,
                "event=release_final_integration_verification_failed path="
                + str(final_integration_verification_path),
            )
    if final_integration_verification_path is not None and final_integration_verification_summary is None:
        final_integration_verification = json.loads(
            final_integration_verification_path.read_text(encoding="utf-8")
        )
        final_integration_verification_summary = {
            "release_id": final_integration_verification.get("release_id"),
            "integration_branch": final_integration_verification.get("integration_branch"),
            "integration_commit": final_integration_verification.get("integration_commit"),
            "verification_log_path": final_integration_verification.get("verification_log_path"),
            "worktree_log_path": final_integration_verification.get("worktree_log_path"),
            "success": bool(final_integration_verification.get("success")),
            "command_results": [
                {
                    "command": result.get("command"),
                    "exit_code": result.get("exit_code"),
                    "stdout_path": result.get("stdout_path"),
                    "stderr_path": result.get("stderr_path"),
                    "duration_seconds": result.get("duration_seconds"),
                    "timed_out": bool(result.get("timed_out")),
                }
                for result in final_integration_verification.get("command_results", [])
                if isinstance(result, dict)
            ],
            "verified_at": final_integration_verification.get("verified_at"),
        }
    finalization_gate = _build_release_finalization_gate(
        decision=decision,
        feature_review_decision=feature_review_decision,
        feature_review_recheck=feature_review_recheck,
    )
    final_review_continuation_decision_path = _write_final_review_continuation_decision(
        release_root=runs_dir / run_id,
        release_id=release_id,
        feature_review_decision=feature_review_decision,
        feature_review_path=feature_review_path,
        feature_review_recheck=feature_review_recheck,
        feature_review_recheck_path=feature_review_recheck_path,
        feature_review_proposals=feature_review_proposals,
        final_integration_verification_path=final_integration_verification_path,
        final_review_finding_adjudication_paths=final_review_finding_adjudication_paths,
        finalization_gate=finalization_gate,
    )
    _report(
        progress,
        "event=final_review_continuation_decision path="
        + str(final_review_continuation_decision_path),
    )
    compact_memory_path = _persist_compact_final_review_follow_up_memory(
        config_repo_path=config.repo_path,
        repo_state_path=config.repo_state_path,
        release_id=release_id,
        continuation_decision_path=final_review_continuation_decision_path,
    )
    if compact_memory_path is not None:
        _report(progress, "event=repo_state_follow_up_memory path=" + str(compact_memory_path))
    if not bool(finalization_gate["allowed"]):
        _report(
            progress,
            "event=release_finalization_blocked reason="
            + str(finalization_gate["reason"])
            + " unresolved_required_findings="
            + json.dumps(finalization_gate["unresolved_required_finding_ids"], sort_keys=True),
        )
    finalization_decision_path, finalization = _finalize_release(
        release_root=release_root,
        release_id=release_id,
        run_id=run_id,
        repo_path=config.repo_path,
        integration_branch=feature_branch,
        base_branch=config.default_base_branch,
        integration_commit=integration_commit,
        policy=config.release_finalization_policy,
        decision=decision,
        allowed=bool(finalization_gate["allowed"]),
        blocked_reason=str(finalization_gate["reason"]),
        mode=release_finalize,
        progress=progress,
    )
    summary_path = _write_release_summary(
        runs_dir=runs_dir,
        run_id=run_id,
        release_id=release_id,
        decision=decision,
        task_results=task_results,
        log_path=log_path,
        raw_log_path=raw_log_path,
        integration_branch=feature_branch,
        integration_commit=integration_commit,
        finalization=finalization,
        budget_path=budget_path,
        tuning_path=tuning_path,
        budget_violations=budget_violations,
        soft_budget_findings=soft_budget_findings,
        release_soft_gate_decision_path=release_soft_gate_decision_path,
        feature_review_path=feature_review_path,
        feature_review_recheck_path=feature_review_recheck_path,
        feature_review_proposals=feature_review_proposals,
        feature_review_prompt_path=feature_review_prompt_path,
        feature_review_stdout_path=feature_review_stdout_path,
        feature_review_stderr_path=feature_review_stderr_path,
        feature_review_metadata_path=feature_review_metadata_path,
        feature_review_bundle_manifest_paths=feature_review_bundle_manifest_paths,
        feature_review_output_normalization_decision_path=feature_review_output_normalization_decision_path,
        feature_review_normalized_artifact_path=feature_review_normalized_artifact_path,
        final_review_continuation_decision_path=final_review_continuation_decision_path,
        finalization_gate=finalization_gate,
        finalization_decision_path=finalization_decision_path,
        final_integration_verification_path=final_integration_verification_path,
        final_integration_verification=final_integration_verification_summary,
        scope_risk_budget_policy_decision_paths=scope_risk_budget_policy_decision_paths,
        scope_risk_budget_policy_gate=scope_risk_budget_policy_gate,
    )
    review_path = _write_release_review(
        runs_dir=runs_dir,
        run_id=run_id,
        release_id=release_id,
        decision=decision,
        task_results=task_results,
        integration_branch=feature_branch,
        finalization=finalization,
        metrics_path=metrics_path,
        budget_path=budget_path,
        tuning_path=tuning_path,
        budget_violations=budget_violations,
        soft_budget_findings=soft_budget_findings,
        release_soft_gate_decision_path=release_soft_gate_decision_path,
        feature_review_decision=feature_review_decision,
        feature_review_path=feature_review_path,
        feature_review_recheck=feature_review_recheck,
        feature_review_recheck_path=feature_review_recheck_path,
        feature_review_prompt_path=feature_review_prompt_path,
        feature_review_stdout_path=feature_review_stdout_path,
        feature_review_stderr_path=feature_review_stderr_path,
        feature_review_metadata_path=feature_review_metadata_path,
        feature_review_output_normalization_decision_path=feature_review_output_normalization_decision_path,
        feature_review_normalized_artifact_path=feature_review_normalized_artifact_path,
        final_review_continuation_decision_path=final_review_continuation_decision_path,
        finalization_gate=finalization_gate,
        final_integration_verification_path=final_integration_verification_path,
        scope_risk_budget_policy_decision_paths=scope_risk_budget_policy_decision_paths,
        scope_risk_budget_policy_gate=scope_risk_budget_policy_gate,
    )
    _report(progress, f"event=release_decision decision={decision}")
    _report(progress, f"event=release_review path={review_path}")
    _report(progress, f"event=release_metrics path={metrics_path}")
    _report(progress, f"event=release_budget path={budget_path}")
    _report(progress, f"event=release_tuning path={tuning_path}")
    _write_release_log_summary(
        log_path=log_path,
        raw_log_path=raw_log_path,
        release_id=release_id,
        decision=decision,
        task_results=task_results,
        metrics_path=metrics_path,
        budget_path=budget_path,
        tuning_path=tuning_path,
        review_path=review_path,
    )

    return ReleaseRunResult(
        release_id=release_id,
        run_id=run_id,
        summary_path=summary_path,
        log_path=log_path,
        review_path=review_path,
        metrics_path=metrics_path,
        budget_path=budget_path,
        tuning_path=tuning_path,
        task_results=task_results,
        decision=decision,
        integration_branch=feature_branch,
        finalization=finalization,
        finalization_gate=finalization_gate,
        finalization_decision_path=finalization_decision_path,
        feature_review_path=feature_review_path,
        feature_review_recheck_path=feature_review_recheck_path,
        final_review_continuation_decision_path=final_review_continuation_decision_path,
        final_integration_verification_path=final_integration_verification_path,
        feature_review_prompt_path=feature_review_prompt_path,
        feature_review_stdout_path=feature_review_stdout_path,
        feature_review_stderr_path=feature_review_stderr_path,
        feature_review_metadata_path=feature_review_metadata_path,
        feature_review_output_normalization_decision_path=feature_review_output_normalization_decision_path,
        feature_review_normalized_artifact_path=feature_review_normalized_artifact_path,
        feature_review_proposals=feature_review_proposals,
        scope_risk_budget_policy_decision_paths=scope_risk_budget_policy_decision_paths,
        scope_risk_budget_policy_gate=scope_risk_budget_policy_gate,
    )


def _select_contracts(
    *,
    release_id: str,
    config_repo_path: Path,
    repo_state_path: Path | None,
    contracts_dir: Path,
    contract_paths: list[Path] | None,
) -> list[Path]:
    if contract_paths:
        return contract_paths

    release_plan = _load_release_plan(config_repo_path, repo_state_path)
    if release_plan is not None and release_plan.release_id == release_id:
        return [contracts_dir / f"{task_id}.yaml" for task_id in release_plan.current_tasks]

    contracts = []
    for path in sorted(contracts_dir.glob("*.yaml")):
        task = load_yaml_model(path, TaskContract)
        if task.release_id == release_id:
            contracts.append(path)
    return contracts


def _feature_review_source_contracts(
    *,
    release_id: str,
    contracts_dir: Path,
    selected_tasks: list[TaskContract],
) -> list[TaskContract]:
    """Feature review sees the whole feature diff, including prior release slices."""
    by_task_id = {task.task_id: task for task in selected_tasks}
    for path in sorted(contracts_dir.glob("*.yaml")):
        try:
            task = load_yaml_model(path, TaskContract)
        except Exception:
            continue
        if task.release_id == release_id:
            by_task_id.setdefault(task.task_id, task)
    return [by_task_id[task_id] for task_id in sorted(by_task_id)]


def _release_objective_from_contracts(source_contracts: list[TaskContract]) -> str | None:
    objectives = [
        task.objective.strip()
        for task in source_contracts
        if isinstance(task.objective, str) and task.objective.strip()
    ]
    if not objectives:
        return None
    if len(set(objectives)) == 1:
        return objectives[0]
    return " / ".join(dict.fromkeys(objectives))


def collect_release_planning_state_review_snapshot(
    *,
    config_repo_path: Path,
    repo_state_path: Path | None,
    runs_dir: Path,
    planning_artifacts_dir: Path,
    objective_path: Path | None = None,
    context_bundle_max_chars: int = 50_000,
    now: datetime | None = None,
) -> Path:
    if not planning_artifacts_dir.exists():
        raise ValueError(
            f"release planning artifacts directory does not exist: {planning_artifacts_dir}"
        )
    snapshot = collect_state_review_snapshot(
        repo_path=config_repo_path,
        repo_state_path=repo_state_path,
        runs_dir=runs_dir,
        now=now,
    )
    snapshot_path = write_state_review_snapshot_artifact(
        snapshot=snapshot,
        artifacts_dir=planning_artifacts_dir,
    )

    write_state_review_context_bundle(
        snapshot=snapshot,
        state_review_snapshot_path=snapshot_path,
        runs_dir=runs_dir,
        artifacts_dir=planning_artifacts_dir,
        objective_path=objective_path,
        max_chars=context_bundle_max_chars,
        now=now,
    )
    return snapshot_path


def _run_release_sequential(
    *,
    project_id: str,
    config_repo_path: Path,
    config_dir: Path,
    runs_dir: Path,
    release_run_dir: Path,
    release_id: str,
    run_id: str,
    repo_state_path: Path | None,
    task_base_branch: str,
    task_inputs: list[tuple[Path, TaskContract]],
    executor: ExecutorProtocol | None,
    verification_timeout_seconds: int,
    allow_dirty: bool,
    commit_on_accept: bool,
    merge_on_accept: bool,
    push_on_accept: bool,
    stop_on_failure: bool,
    debug_keep_artifacts: bool,
    progress: Callable[[str], None] | None,
) -> list[TaskRunResult]:
    supervisor_dir = release_run_dir / "runtime_supervisor"
    supervisor_dir.mkdir(parents=True, exist_ok=True)
    supervisor_log_path = supervisor_dir / "supervisor.log"
    supervisor = RuntimeSupervisor()
    supervisor_max_retries = max(0, min(3, load_project_config(project_id, config_dir, validate_repo=True).budget.max_executor_attempts_per_task))
    task_results: list[TaskRunResult] = []
    for index, (contract_path, task) in enumerate(task_inputs, start=1):
        _report(
            progress,
            f"event=task_started index={index} total={len(task_inputs)} task={task.task_id} "
            f"title={json.dumps(task.title)} budget={task.budget_class} type={task.task_type}",
        )
        _report(progress, f"event=task_objective task={task.task_id} objective={json.dumps(task.objective)}")
        _report(progress, f"event=task_scope task={task.task_id} allowed={json.dumps(task.allowed_files)}")
        result = _run_one_release_task(
            project_id=project_id,
            config_repo_path=config_repo_path,
            config_dir=config_dir,
            runs_dir=runs_dir,
            task_base_branch=task_base_branch,
            contract_path=contract_path,
            task=task,
            executor=executor,
            verification_timeout_seconds=verification_timeout_seconds,
            allow_dirty=allow_dirty,
            commit_on_accept=commit_on_accept,
            merge_on_accept=merge_on_accept,
            push_on_accept=push_on_accept,
            debug_keep_artifacts=debug_keep_artifacts,
            progress=progress,
        )

        if result.decision.decision != Decision.ACCEPTED:
            result = _attempt_runtime_supervisor_repair_and_resume(
                project_id=project_id,
                config_repo_path=config_repo_path,
                config_dir=config_dir,
                runs_dir=runs_dir,
                release_run_dir=release_run_dir,
                release_id=release_id,
                release_run_id=run_id,
                repo_state_path=repo_state_path,
                task_base_branch=task_base_branch,
                contract_path=contract_path,
                task=task,
                initial_result=result,
                supervisor=supervisor,
                supervisor_log_path=supervisor_log_path,
                max_retries=supervisor_max_retries,
                executor=executor,
                verification_timeout_seconds=verification_timeout_seconds,
                allow_dirty=allow_dirty,
                commit_on_accept=commit_on_accept,
                merge_on_accept=merge_on_accept,
                push_on_accept=push_on_accept,
                debug_keep_artifacts=debug_keep_artifacts,
                progress=progress,
            )
        task_results.append(result)
        if stop_on_failure and result.decision.decision != Decision.ACCEPTED:
            _report(progress, f"stopping release after {task.task_id}: {result.decision.decision}")
            break
    return task_results


def _attempt_runtime_supervisor_repair_and_resume(
    *,
    project_id: str,
    config_repo_path: Path,
    config_dir: Path,
    runs_dir: Path,
    release_run_dir: Path,
    release_id: str,
    release_run_id: str,
    repo_state_path: Path | None,
    task_base_branch: str,
    contract_path: Path,
    task: TaskContract,
    initial_result: TaskRunResult,
    supervisor: RuntimeSupervisor,
    supervisor_log_path: Path,
    max_retries: int,
    executor: ExecutorProtocol | None,
    verification_timeout_seconds: int,
    allow_dirty: bool,
    commit_on_accept: bool,
    merge_on_accept: bool,
    push_on_accept: bool,
    debug_keep_artifacts: bool,
    progress: Callable[[str], None] | None,
) -> TaskRunResult:
    supervisor_dir = release_run_dir / "runtime_supervisor"
    supervisor_dir.mkdir(parents=True, exist_ok=True)
    temp_evidence_dir = supervisor_dir / "temp"
    temp_evidence_dir.mkdir(parents=True, exist_ok=True)

    retry_budget_path = supervisor_dir / "retry_budget_ledger.json"
    repair_budget_path = supervisor_dir / "repair_budget_ledger.json"
    model_tuning_path = supervisor_dir / "model_tuning_report.json"
    verification_tuning_path = supervisor_dir / "verification_tuning_report.json"
    for path in (retry_budget_path, repair_budget_path, model_tuning_path, verification_tuning_path):
        if not path.exists():
            path.write_text("[]\n" if path.name.endswith("ledger.json") else "{}\n", encoding="utf-8")

    backlog_state_reference = _runtime_supervisor_backlog_state_reference(
        config_repo_path=config_repo_path,
        repo_state_path=repo_state_path,
        supervisor_dir=supervisor_dir,
        active_epic_id=release_id,
    )

    repair_evidence_path = supervisor_dir / f"repair_{task.task_id}.json"
    repair_evidence: dict[str, object] = {
        "release_id": release_id,
        "release_run_id": release_run_id,
        "task_id": task.task_id,
        "contract_path": str(contract_path),
        "initial_result": _task_result_reference(initial_result),
        "attempts": [],
        "final_result": None,
    }

    current_result = initial_result
    attempt = 1
    while attempt <= max_retries:
        classification, event_kind, failure_category = _runtime_supervisor_classification_for_task_result(
            result=current_result,
            task=task,
        )
        if classification is None:
            break

        _report(
            progress,
            f"event=repair_attempt_started task={task.task_id} attempt={attempt} classification={classification}",
        )
        _append_supervisor_log(
            supervisor_log_path,
            f"event=repair_attempt_started task={task.task_id} attempt={attempt} classification={classification}\n",
        )

        release_event = _runtime_supervisor_write_release_event(
            supervisor_dir=supervisor_dir,
            task_id=task.task_id,
            attempt=attempt,
            kind=event_kind,
            message=f"task={task.task_id} decision={current_result.decision.decision} category={failure_category}",
        )
        release_summary_ref = _runtime_supervisor_write_release_state_summary(
            supervisor_dir=supervisor_dir,
            release_id=release_id,
            release_run_id=release_run_id,
            task=task,
            attempt=attempt,
            result=current_result,
        )
        evidence_paths = _runtime_supervisor_evidence_paths(current_result)
        raw_log_paths = RawLogPaths(
            supervisor_log_path=supervisor_log_path,
            worker_stdout_path=evidence_paths.bundle_path / "executor_stdout.log",
            worker_stderr_path=evidence_paths.bundle_path / "executor_stderr.log",
        )
        budgets = BudgetLedgerPaths(
            repair_budget_ledger_path=repair_budget_path,
            retry_budget_ledger_path=retry_budget_path,
        )
        tuning = TuningReportPaths(
            model_tuning_report_path=model_tuning_path,
            verification_tuning_report_path=verification_tuning_path,
        )
        supervisor_input = RuntimeSupervisorInput(
            classification=classification,
            attempt=attempt,
            max_retries=max_retries,
            release_event=release_event,
            release_summary=release_summary_ref,
            evidence_bundle_paths=EvidenceBundlePaths(
                bundle_path=evidence_paths.bundle_path,
                changed_files_path=evidence_paths.changed_files_path,
                verification_log_path=evidence_paths.verification_log_path,
            ),
            raw_log_paths=raw_log_paths,
            budget_ledger_paths=budgets,
            tuning_report_paths=tuning,
            backlog_state_reference=backlog_state_reference,
        )
        decision = supervisor.decide(supervisor_input)
        _report(
            progress,
            f"event=repair_decision task={task.task_id} attempt={attempt} decision={decision.decision} action={decision.action.action_kind if decision.action else None}",
        )
        repair_attempt_record: dict[str, object] = {
            "attempt": attempt,
            "classification": str(decision.classification),
            "decision": str(decision.decision),
            "retryable": bool(decision.retryable),
            "reason": decision.reason,
            "remaining_retries": decision.remaining_retries,
            "action_kind": str(decision.action.action_kind) if decision.action else None,
            "result": _task_result_reference(current_result),
        }

        if decision.decision != RuntimeSupervisorDecisionKind.RETRY or decision.action is None:
            repair_attempt_record["stop_reason"] = str(decision.stop_reason) if decision.stop_reason else None
            repair_evidence["attempts"].append(repair_attempt_record)
            _runtime_supervisor_write_repair_evidence(repair_evidence_path, repair_evidence)
            _report(progress, f"event=repair_stopped task={task.task_id} reason={json.dumps(decision.reason)}")
            return current_result

        if decision.action.action_kind != RepairActionKind.RELEASE_RESUME:
            repair_attempt_record["unsupported_action"] = str(decision.action.action_kind)
            repair_evidence["attempts"].append(repair_attempt_record)
            _runtime_supervisor_write_repair_evidence(repair_evidence_path, repair_evidence)
            _report(
                progress,
                f"event=repair_unsupported task={task.task_id} action={decision.action.action_kind}",
            )
            return current_result

        # Validate release resume intent via supervisor applier.
        resume_action_id = f"release_resume/{task.task_id}/attempt-{attempt}"
        resume_budget = max(0, max_retries - attempt)
        applier_result = supervisor.apply_release_resume_intent(
            source_evidence_paths=decision.action.source_evidence_paths,
            action_id=resume_action_id,
            retry_budget=resume_budget,
            stop_reason_fallback=RuntimeSupervisorStopReason.EXHAUSTED_RETRY_BUDGET,
        )
        repair_attempt_record["applier_applied"] = bool(applier_result.applied)
        repair_attempt_record["applier_stop_evidence"] = (
            {
                "action_kind": str(applier_result.stop_evidence.action_kind),
                "kind": str(applier_result.stop_evidence.kind),
                "reason": applier_result.stop_evidence.reason,
            }
            if applier_result.stop_evidence
            else None
        )
        if not applier_result.applied or applier_result.proposal is None:
            repair_evidence["attempts"].append(repair_attempt_record)
            _runtime_supervisor_write_repair_evidence(repair_evidence_path, repair_evidence)
            _report(progress, f"event=repair_resume_blocked task={task.task_id}")
            return current_result

        # Stop if the task already violated its allowed_files scope.
        changed_files = _runtime_supervisor_read_changed_files(evidence_paths.changed_files_path)
        if any(not _path_allowed(path, task.allowed_files) for path in changed_files):
            repair_attempt_record["blocked_reason"] = "contract_boundary_violation"
            repair_attempt_record["changed_files"] = changed_files
            repair_evidence["attempts"].append(repair_attempt_record)
            repair_evidence["final_result"] = _task_result_reference(current_result)
            _runtime_supervisor_write_repair_evidence(repair_evidence_path, repair_evidence)
            _report(progress, f"event=repair_boundary_violation task={task.task_id}")
            return current_result

        repair_evidence["attempts"].append(repair_attempt_record)
        _runtime_supervisor_write_repair_evidence(repair_evidence_path, repair_evidence)
        _report(progress, f"event=task_resumed task={task.task_id} attempt={attempt}")

        # Resume by rerunning the same task contract with full verification/review.
        current_result = _run_one_release_task(
            project_id=project_id,
            config_repo_path=config_repo_path,
            config_dir=config_dir,
            runs_dir=runs_dir,
            task_base_branch=task_base_branch,
            contract_path=contract_path,
            task=task,
            executor=executor,
            verification_timeout_seconds=verification_timeout_seconds,
            allow_dirty=allow_dirty,
            commit_on_accept=commit_on_accept,
            merge_on_accept=merge_on_accept,
            push_on_accept=push_on_accept,
            debug_keep_artifacts=debug_keep_artifacts,
            progress=progress,
        )
        if current_result.decision.decision == Decision.ACCEPTED:
            repair_evidence["final_result"] = _task_result_reference(current_result)
            _runtime_supervisor_write_repair_evidence(repair_evidence_path, repair_evidence)
            _report(progress, f"event=repair_succeeded task={task.task_id} attempt={attempt}")
            return current_result

        attempt += 1

    # Retry budget exhausted or unsupported.
    repair_evidence["final_result"] = _task_result_reference(current_result)
    _runtime_supervisor_write_repair_evidence(repair_evidence_path, repair_evidence)
    _report(progress, f"event=repair_exhausted task={task.task_id} attempts={max_retries}")
    return current_result


def _append_supervisor_log(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _runtime_supervisor_write_repair_evidence(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _task_result_reference(result: TaskRunResult) -> dict[str, object]:
    return {
        "run_id": result.run_id,
        "bundle_path": str(result.bundle_path),
        "decision": str(result.decision.decision),
        "rationale": result.decision.rationale,
    }


def _runtime_supervisor_evidence_paths(result: TaskRunResult) -> EvidenceBundlePaths:
    bundle_path = result.bundle_path
    return EvidenceBundlePaths(
        bundle_path=bundle_path,
        changed_files_path=bundle_path / "changed_files.txt",
        verification_log_path=bundle_path / "verification.log",
    )


def _runtime_supervisor_read_changed_files(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _runtime_supervisor_backlog_state_reference(
    *,
    config_repo_path: Path,
    repo_state_path: Path | None,
    supervisor_dir: Path,
    active_epic_id: str,
) -> BacklogStateReference:
    if repo_state_path is None:
        placeholder = supervisor_dir / "backlog_state_missing.txt"
        placeholder.write_text("repo_state_path is not configured for this project.\n", encoding="utf-8")
        return BacklogStateReference(backlog_state_path=placeholder, active_epic_id=active_epic_id)
    root = repo_state_path if repo_state_path.is_absolute() else config_repo_path / repo_state_path
    backlog_path = root / "backlog_state.yaml"
    if backlog_path.exists():
        return BacklogStateReference(backlog_state_path=backlog_path, active_epic_id=active_epic_id)
    placeholder = supervisor_dir / "backlog_state_missing.txt"
    placeholder.write_text(f"Missing backlog_state.yaml at {backlog_path}.\n", encoding="utf-8")
    return BacklogStateReference(backlog_state_path=placeholder, active_epic_id=active_epic_id)


def _runtime_supervisor_write_release_event(
    *,
    supervisor_dir: Path,
    task_id: str,
    attempt: int,
    kind: ReleaseEventKind,
    message: str,
) -> ReleaseEvent:
    event_dir = supervisor_dir / "events"
    event_dir.mkdir(parents=True, exist_ok=True)
    event_path = event_dir / f"{task_id}_attempt_{attempt}.json"
    event_payload = {
        "kind": str(kind),
        "task_id": task_id,
        "attempt": attempt,
        "message": message,
    }
    event_path.write_text(json.dumps(event_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return ReleaseEvent(kind=kind, message=message, event_path=event_path)


def _runtime_supervisor_write_release_state_summary(
    *,
    supervisor_dir: Path,
    release_id: str,
    release_run_id: str,
    task: TaskContract,
    attempt: int,
    result: TaskRunResult,
) -> ReleaseSummaryReference:
    summary_path = supervisor_dir / "release_state.json"
    payload = {
        "release_id": release_id,
        "release_run_id": release_run_id,
        "task_id": task.task_id,
        "attempt": attempt,
        "decision": str(result.decision.decision),
        "bundle_path": str(result.bundle_path),
    }
    summary_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return ReleaseSummaryReference(release_id=release_id, summary_path=summary_path)


def _runtime_supervisor_classification_for_task_result(
    *,
    result: TaskRunResult,
    task: TaskContract,
) -> tuple[RepairDecisionClassification | None, ReleaseEventKind, str]:
    category = "unknown"
    diagnosis_path = result.bundle_path / "failure_diagnosis.yaml"
    if diagnosis_path.exists():
        try:
            import yaml

            diagnosis = yaml.safe_load(diagnosis_path.read_text(encoding="utf-8")) or {}
            category = str(diagnosis.get("category") or category)
        except Exception:
            category = "unknown"

    if result.decision.decision == Decision.ESCALATED:
        return (RepairDecisionClassification.UNSAFE_POLICY_EXPANSION, ReleaseEventKind.RELEASE_BLOCKED, category)
    if category == "contract_mismatch":
        return (RepairDecisionClassification.CONTRACT_BOUNDARY_VIOLATION, ReleaseEventKind.RELEASE_BLOCKED, category)
    if category == "verification_failure":
        repair_decision = _load_environment_repair_decision(result.bundle_path)
        if repair_decision is not None:
            if repair_decision.outcome == EnvironmentRepairOutcome.STOP:
                return (
                    RepairDecisionClassification.UNSAFE_POLICY_EXPANSION,
                    ReleaseEventKind.RELEASE_BLOCKED,
                    category,
                )
            if repair_decision.outcome in {EnvironmentRepairOutcome.APPLY_AND_RETRY, EnvironmentRepairOutcome.CAPTURE_ONLY}:
                return (
                    RepairDecisionClassification.EXHAUSTED_RETRY_BUDGET,
                    ReleaseEventKind.RELEASE_BLOCKED,
                    category,
                )
        return (RepairDecisionClassification.RELEASE_RESUMABLE, ReleaseEventKind.VERIFICATION_FAILED, category)
    if category == "timeout":
        return (RepairDecisionClassification.TASK_SCOPE_OVERBROAD, ReleaseEventKind.TASK_FAILED, category)
    if category == "model_quota":
        return (RepairDecisionClassification.MISSING_CREDENTIALS, ReleaseEventKind.RELEASE_BLOCKED, category)
    if category == "executor_error":
        return (RepairDecisionClassification.RELEASE_RESUMABLE, ReleaseEventKind.TASK_FAILED, category)

    # Fallback: retry only for needs_revision/failed, otherwise stop.
    if result.decision.decision in {Decision.NEEDS_REVISION, Decision.FAILED}:
        return (RepairDecisionClassification.RELEASE_RESUMABLE, ReleaseEventKind.TASK_FAILED, category)
    return (None, ReleaseEventKind.TASK_FAILED, category)


def _load_environment_repair_decision(bundle_path: Path) -> EnvironmentRepairDecision | None:
    decision_dir = bundle_path / "supervisor_decisions"
    if not decision_dir.exists():
        return None
    candidates = sorted(decision_dir.glob("environment_repair__*.json"))
    if not candidates:
        return None
    try:
        loaded = load_supervisor_decision_artifact(candidates[-1])
    except Exception:
        return None
    if isinstance(loaded, EnvironmentRepairDecision):
        return loaded
    return None


def _path_allowed(path: str, allowed_patterns: list[str]) -> bool:
    normalized = path.lstrip("./")
    return any(fnmatch(normalized, pattern) for pattern in allowed_patterns)


def _run_release_parallel(
    *,
    project_id: str,
    config_repo_path: Path,
    config_dir: Path,
    runs_dir: Path,
    task_base_branch: str,
    task_inputs: list[tuple[Path, TaskContract]],
    dependencies: dict[str, list[str]],
    completed_task_ids: set[str],
    executor: ExecutorProtocol | None,
    verification_timeout_seconds: int,
    allow_dirty: bool,
    commit_on_accept: bool,
    merge_on_accept: bool,
    push_on_accept: bool,
    stop_on_failure: bool,
    debug_keep_artifacts: bool,
    progress: Callable[[str], None] | None,
) -> list[TaskRunResult]:
    by_task_id = {task.task_id: (path, task) for path, task in task_inputs}
    pending = set(by_task_id)
    completed: set[str] = set(completed_task_ids)
    failed = False
    task_results_by_id: dict[str, TaskRunResult] = {}
    futures: dict[Future[TaskRunResult], str] = {}
    max_workers = max(1, len(task_inputs))
    _report(progress, f"event=parallel_scheduler max_workers={max_workers}")
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        while pending or futures:
            ready = sorted(
                task_id
                for task_id in pending
                if not (stop_on_failure and failed)
                and all(dependency in completed for dependency in dependencies.get(task_id, []))
            )
            for task_id in ready:
                contract_path, task = by_task_id[task_id]
                pending.remove(task_id)
                _report(
                    progress,
                    f"event=task_submitted task={task_id} title={json.dumps(task.title)} "
                    f"budget={task.budget_class} type={task.task_type}",
                )
                _report(progress, f"event=task_objective task={task_id} objective={json.dumps(task.objective)}")
                _report(progress, f"event=task_scope task={task_id} allowed={json.dumps(task.allowed_files)}")
                futures[
                    pool.submit(
                        _run_one_release_task,
                        project_id=project_id,
                        config_repo_path=config_repo_path,
                        config_dir=config_dir,
                        runs_dir=runs_dir,
                        task_base_branch=task_base_branch,
                        contract_path=contract_path,
                        task=task,
                        executor=executor,
                        verification_timeout_seconds=verification_timeout_seconds,
                        allow_dirty=allow_dirty,
                        commit_on_accept=commit_on_accept,
                        merge_on_accept=merge_on_accept,
                        push_on_accept=push_on_accept,
                        debug_keep_artifacts=debug_keep_artifacts,
                        progress=progress,
                    )
                ] = task_id

            if not futures:
                blocked = ", ".join(sorted(pending))
                if failed and stop_on_failure:
                    _report(progress, f"event=parallel_scheduler_stopped pending={json.dumps(blocked)}")
                    break
                raise ValueError(f"release execution DAG has unsatisfied dependencies: {blocked}")

            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                task_id = futures.pop(future)
                result = future.result()
                task_results_by_id[task_id] = result
                completed.add(task_id)
                _report(progress, f"event=task_completed task={task_id} decision={result.decision.decision}")
                if result.decision.decision != Decision.ACCEPTED:
                    failed = True

    return [task_results_by_id[task.task_id] for _, task in task_inputs if task.task_id in task_results_by_id]


def _run_one_release_task(
    *,
    project_id: str,
    config_repo_path: Path,
    config_dir: Path,
    runs_dir: Path,
    task_base_branch: str,
    contract_path: Path,
    task: TaskContract,
    executor: ExecutorProtocol | None,
    verification_timeout_seconds: int,
    allow_dirty: bool,
    commit_on_accept: bool,
    merge_on_accept: bool,
    push_on_accept: bool,
    debug_keep_artifacts: bool,
    progress: Callable[[str], None] | None,
) -> TaskRunResult:
    result = run_task(
        project_id=project_id,
        contract_path=contract_path,
        config_dir=config_dir,
        runs_dir=runs_dir,
        executor=executor,
        verification_timeout_seconds=verification_timeout_seconds,
        allow_dirty=allow_dirty,
        commit_on_accept=commit_on_accept,
        merge_on_accept=merge_on_accept,
        push_on_accept=push_on_accept,
        commit_message=f"{task.task_id}: {task.title}",
        base_branch=task_base_branch,
        progress=progress,
    )
    if debug_keep_artifacts:
        _report(progress, f"debug_keep_artifacts=true worktree={result.worktree_path}")
    else:
        for message in cleanup_task_artifacts(
            repo_path=config_repo_path,
            worktree_path=result.worktree_path,
            branch=f"agent/{task.release_id}/{task.task_id}",
            preserve_worktree=_should_preserve_task_worktree(result),
            preserve_branch=_should_preserve_task_branch(result),
        ):
            _report(progress, message)
    return result


def _run_feature_review_and_repair_loop(
    *,
    project_id: str,
    config: "ProjectConfig",
    release_id: str,
    run_id: str,
    release_root: Path,
    runs_dir: Path,
    config_dir: Path,
    integration_branch: str,
    task_results: list[TaskRunResult],
    source_contracts: list[TaskContract],
    executor: ExecutorProtocol | None,
    verification_timeout_seconds: int,
    allow_dirty: bool,
    commit_on_accept: bool,
    merge_on_accept: bool,
    push_on_accept: bool,
    debug_keep_artifacts: bool,
    progress: Callable[[str], None] | None,
    max_feature_review_repair_loops_override: int | None = None,
) -> FeatureReviewLoopResult:
    output_root = release_root / "feature_review"
    output_root.mkdir(parents=True, exist_ok=True)
    reviewer_config = config.model_roles.get("reviewer", config.executor)

    gating_decision = Decision.ACCEPTED
    all_task_results: list[TaskRunResult] = []
    feature_review_path: Path | None = None
    feature_review_recheck_path: Path | None = None
    feature_review_decision: FeatureReviewDecision | None = None
    feature_review_recheck: FeatureReviewRecheckRecord | None = None
    feature_review_prompt_path: Path | None = None
    feature_review_stdout_path: Path | None = None
    feature_review_stderr_path: Path | None = None
    feature_review_metadata_path: Path | None = None
    feature_review_bundle_manifest_paths: list[Path] = []
    feature_review_output_normalization_decision_path: Path | None = None
    feature_review_normalized_artifact_path: Path | None = None
    outstanding_required_finding_ids: set[str] = set()
    previous_review_decisions: list[FeatureReviewDecision] = []
    last_verification_ok = False
    last_verification_log_path: Path | None = None
    proposal_by_finding_id: dict[str, FeatureReviewProposalRecord] = {}
    final_integration_verification_path: Path | None = None
    final_review_finding_adjudication_paths: list[Path] = []

    def allowed_verification_commands() -> list[str]:
        commands: list[str] = []
        for profile in config.verification_profiles.values():
            commands.extend(profile.commands)
        results = all_task_results or task_results
        for result in results:
            run_state = _read_json_object(result.bundle_path / "run_state.json")
            verification_results = run_state.get("verification_results", [])
            if not isinstance(verification_results, list):
                continue
            for item in verification_results:
                if not isinstance(item, dict):
                    continue
                command = item.get("command")
                if isinstance(command, str) and command.strip():
                    commands.append(command.strip())
        seen: set[str] = set()
        ordered: list[str] = []
        for command in commands:
            normalized = str(command).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered

    def integration_verification_commands() -> list[str]:
        default_profile = config.verification_profiles.get("default")
        commands = list(default_profile.commands) if default_profile is not None else []
        return commands or ["integration_verification"]

    def current_proposals() -> list[FeatureReviewProposalRecord]:
        return sorted(
            proposal_by_finding_id.values(),
            key=lambda record: record.finding_id,
        )

    def build_result() -> FeatureReviewLoopResult:
        return FeatureReviewLoopResult(
            task_results=all_task_results,
            feature_review_path=feature_review_path,
            feature_review_recheck_path=feature_review_recheck_path,
            feature_review_decision=feature_review_decision,
            feature_review_recheck=feature_review_recheck,
            feature_review_proposals=current_proposals(),
            gating_decision=gating_decision,
            final_integration_verification_path=final_integration_verification_path,
            final_review_finding_adjudication_paths=list(final_review_finding_adjudication_paths),
            feature_review_prompt_path=feature_review_prompt_path,
            feature_review_stdout_path=feature_review_stdout_path,
            feature_review_stderr_path=feature_review_stderr_path,
            feature_review_metadata_path=feature_review_metadata_path,
            feature_review_bundle_manifest_paths=list(feature_review_bundle_manifest_paths),
            feature_review_output_normalization_decision_path=feature_review_output_normalization_decision_path,
            feature_review_normalized_artifact_path=feature_review_normalized_artifact_path,
        )

    def run_review(attempt: int) -> FeatureReviewDecision:
        nonlocal feature_review_path
        nonlocal feature_review_decision
        nonlocal feature_review_prompt_path
        nonlocal feature_review_stdout_path
        nonlocal feature_review_stderr_path
        nonlocal feature_review_metadata_path
        nonlocal feature_review_bundle_manifest_paths
        nonlocal feature_review_output_normalization_decision_path
        nonlocal feature_review_normalized_artifact_path
        attempt_dir = output_root / f"attempt_{attempt:02d}"
        _report(progress, f"event=feature_review_started attempt={attempt} output_dir={attempt_dir}")
        branches_base = config.default_base_branch
        try:
            context = assemble_feature_review_context(
                repo_path=config.repo_path,
                release_id=release_id,
                base_branch=branches_base,
                integration_branch=integration_branch,
                runs_dir=runs_dir,
                release_objective=_release_objective_from_contracts(source_contracts),
            )
            prompt, manifest = render_feature_review_prompt_bundle(
                context=context,
                repo_path=config.repo_path,
                runs_dir=runs_dir,
            )
            attempt_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = attempt_dir / "feature_review_bundle_manifest.json"
            prompt_path = attempt_dir / "feature_review_prompt.md"
            manifest["artifact_paths"]["prompt_path"] = str(prompt_path)
            manifest["artifact_paths"]["bundle_manifest_path"] = str(manifest_path)
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            feature_review_bundle_manifest_paths.append(manifest_path)
            backend = invoke_feature_reviewer(
                config=reviewer_config,
                repo_path=config.repo_path,
                prompt=prompt,
                release_id=release_id,
                output_dir=attempt_dir,
            )
            feature_review_prompt_path = getattr(backend, "prompt_path", None)
            feature_review_stdout_path = getattr(backend, "stdout_path", None)
            feature_review_stderr_path = getattr(backend, "stderr_path", None)
            feature_review_metadata_path = getattr(backend, "metadata_path", None)
            try:
                loaded_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded_manifest = None
            if isinstance(loaded_manifest, dict):
                if feature_review_stdout_path is not None:
                    loaded_manifest.setdefault("artifact_paths", {})["stdout_path"] = str(feature_review_stdout_path)
                if feature_review_stderr_path is not None:
                    loaded_manifest.setdefault("artifact_paths", {})["stderr_path"] = str(feature_review_stderr_path)
                if feature_review_metadata_path is not None:
                    loaded_manifest.setdefault("artifact_paths", {})["metadata_path"] = str(feature_review_metadata_path)
                loaded_manifest.setdefault("artifact_paths", {})["review_output_dir"] = str(attempt_dir)
                manifest_path.write_text(json.dumps(loaded_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            (
                decision,
                feature_review_output_normalization_decision_path,
                feature_review_normalized_artifact_path,
            ) = _normalize_feature_review_model_output_if_needed(
                release_id=release_id,
                release_root=release_root,
                context=context,
                backend=backend,
                fallback_decision=backend.decision,
                progress=progress,
            )
        except FeatureReviewContextError as error:
            decision = FeatureReviewDecision.model_validate(
                {
                    "release_id": release_id,
                    "reviewer": "deterministic",
                    "summary": f"Feature review context failure: {error}",
                    "recommendation": "escalate",
                    "accepted_risks": [],
                    "rerun_verification_commands": [],
                    "findings": [],
                }
            )
            feature_review_prompt_path = None
            feature_review_stdout_path = None
            feature_review_stderr_path = None
            feature_review_metadata_path = None
            feature_review_output_normalization_decision_path = None
            feature_review_normalized_artifact_path = None
        feature_review_decision = decision
        feature_review_path = write_feature_review_decision(release_root, decision)
        _report(progress, f"event=feature_review_completed attempt={attempt} recommendation={decision.recommendation.value}")
        return decision

    def rerun_verification(attempt: int, decision: FeatureReviewDecision) -> bool:
        nonlocal last_verification_ok
        nonlocal last_verification_log_path
        rerun_dir = output_root / f"verification_rerun_{attempt:02d}"
        rerun_dir.mkdir(parents=True, exist_ok=True)
        commands = integration_verification_commands()
        allowed = set(allowed_verification_commands())
        if decision.rerun_verification_commands:
            unknown = [cmd for cmd in decision.rerun_verification_commands if cmd not in allowed]
            if unknown:
                _report(
                    progress,
                    "event=feature_review_verification_commands_ignored commands="
                    + json.dumps(unknown, sort_keys=True),
                )
            requested = [cmd for cmd in decision.rerun_verification_commands if cmd in allowed]
            if requested:
                commands = requested
        log_path = rerun_dir / "verification.log"
        ok = _run_integration_verification_rerun(
            repo_path=config.repo_path,
            integration_branch=integration_branch,
            worktree_path=rerun_dir / "worktree",
            commands=commands,
            timeout_seconds=verification_timeout_seconds,
            log_path=log_path,
            progress=progress,
        )
        last_verification_ok = ok
        last_verification_log_path = log_path
        return ok

    all_task_results = list(task_results)
    decision = run_review(attempt=1)
    previous_review_decisions.append(decision)

    max_feature_review_repair_loops = (
        max_feature_review_repair_loops_override
        if max_feature_review_repair_loops_override is not None
        else config.feature_review_max_repair_loops
    )
    for loop_index in range(max_feature_review_repair_loops + 1):
        required_findings = [finding for finding in decision.findings if finding.required_repairs]
        if required_findings:
            outstanding_required_finding_ids.update(finding.finding_id for finding in required_findings)
        optional_findings = [
            finding
            for finding in decision.findings
            if not finding.required_repairs and finding.optional_follow_ups
        ]

        def compute_convergence():
            return classify_feature_review_findings_for_convergence(
                decision=decision,
                previous_decisions=previous_review_decisions[:-1],
                verification_passed=last_verification_ok,
            )

        try:
            convergence = compute_convergence()
        except FeatureReviewClassificationError as error:
            gating_decision = Decision.ESCALATED
            decision = decision.model_copy(
                update={
                    "reviewer": "deterministic",
                    "summary": f"Feature review finding classification failed: {error}",
                    "recommendation": "escalate",
                }
            )
            feature_review_decision = decision
            feature_review_path = write_feature_review_decision(release_root, decision)
            unresolved_finding_ids = (
                [finding.finding_id for finding in decision.findings]
                or [f"{release_id}:feature_review_classification_failed"]
            )
            unresolved_finding_ids.insert(0, f"{release_id}:schema_invalid_reviewer_output")
            feature_review_recheck = FeatureReviewRecheckRecord(
                release_id=release_id,
                unresolved_finding_ids=unresolved_finding_ids,
                resolved_finding_ids=[],
                accepted_finding_ids=[],
                stop_reason="blocked_by_hard_gate",
            )
            feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
            return build_result()

        def optional_recheck_ids() -> tuple[list[str], list[str]]:
            optional_finding_ids = {finding.finding_id for finding in optional_findings}
            accepted_ids = sorted(optional_finding_ids.intersection(convergence.accepted_finding_ids))
            deferred_ids = sorted(optional_finding_ids.intersection(convergence.deferred_finding_ids))
            return accepted_ids, deferred_ids

        def write_required_finding_classifications(
            *,
            attempt: int,
            accepted_required_finding_ids: set[str] | None = None,
            write_stable_latest: bool = True,
        ) -> dict[str, Path]:
            accepted_required_finding_ids = accepted_required_finding_ids or set()
            written: dict[str, Path] = {}
            validators_to_rerun = integration_verification_commands()
            for finding in required_findings:
                finding_id = finding.finding_id
                is_accepted = finding_id in accepted_required_finding_ids
                classification = (
                    FeatureReviewFindingClassification.FALSE_POSITIVE
                    if is_accepted
                    else FeatureReviewFindingClassification.BLOCKER
                )
                selected_action = (
                    FeatureReviewFindingAction.ACCEPT if is_accepted else FeatureReviewFindingAction.REPAIR
                )
                outcome = FeatureReviewFindingOutcome.CONTINUE
                evidence_paths: list[str] = []
                if feature_review_path is not None:
                    evidence_paths.append(str(feature_review_path.resolve()))
                if last_verification_log_path is not None and (write_stable_latest or is_accepted):
                    evidence_paths.append(str(last_verification_log_path.resolve()))
                stable_decision_id = f"{release_id}__feature_review_finding__{finding_id}"
                attempt_decision_id = f"{stable_decision_id}__attempt_{attempt}"
                decision_record = FeatureReviewFindingClassificationDecision.model_validate(
                    {
                        "decision_id": attempt_decision_id,
                        "release_id": release_id,
                        "decided_at": datetime.now(UTC),
                        "decided_by": "run_release_feature_review_loop",
                        "rationale": (
                            "Classified required finding as verification-only false positive after passing configured "
                            "integration verification rerun; stop generating repair contracts for this finding."
                            if is_accepted
                            else "Classified required finding as release-blocking; generate bounded repair contract."
                        ),
                        "evidence_paths": evidence_paths,
                        "finding_id": finding_id,
                        "classification": classification.value,
                        "selected_action": selected_action.value,
                        "outcome": outcome.value,
                        "fallback_plan": "Stop release finalization and escalate if blockers remain unresolved.",
                        "validators_to_rerun": validators_to_rerun,
                    }
                )
                attempt_path = write_supervisor_decision_artifact(
                    release_bundle_path=release_root, decision=decision_record
                )
                written[finding_id] = attempt_path

                if write_stable_latest:
                    stable_record = decision_record.model_copy(update={"decision_id": stable_decision_id})
                    write_supervisor_decision_artifact(release_bundle_path=release_root, decision=stable_record)
                _report(
                    progress,
                    "event=feature_review_finding_classified attempt="
                    + str(attempt)
                    + " finding_id="
                    + finding_id
                    + " classification="
                    + classification.value
                    + " action="
                    + selected_action.value,
                )
            return written

        def write_non_blocking_finding_classifications(*, attempt: int) -> dict[str, Path]:
            written: dict[str, Path] = {}
            non_blocking_items = [
                item for item in convergence.findings if item.finding_id not in convergence.blocking_finding_ids
            ]
            if not non_blocking_items:
                return written
            for item in non_blocking_items:
                if item.selected_action == "accept":
                    outcome = FeatureReviewFindingOutcome.CONTINUE
                else:
                    outcome = FeatureReviewFindingOutcome.STOP_FINDING
                evidence_paths: list[str] = []
                if feature_review_path is not None:
                    evidence_paths.append(str(feature_review_path.resolve()))
                match_hint = item.matched_previous_finding_id or "none"
                rationale = (
                    "Classified non-blocking finding with convergence context; "
                    f"classification={item.classification} action={item.selected_action} "
                    "defer_outcome_scope=stop_pursuing_this_finding "
                    f"matched_previous_finding_id={match_hint} "
                    f"repeated_by_finding_id={str(item.repeated_by_finding_id).lower()} "
                    f"adjacent_similarity={item.adjacent_similarity:.3f}."
                )
                fallback_plan = (
                    "Rerun reviewer re-check and deterministic finalization gate if the same finding escalates or "
                    "new blocking evidence appears."
                )
                stable_decision_id = f"{release_id}__feature_review_finding__{item.finding_id}"
                attempt_decision_id = f"{stable_decision_id}__attempt_{attempt}"
                decision_record = FeatureReviewFindingClassificationDecision.model_validate(
                    {
                        "decision_id": attempt_decision_id,
                        "release_id": release_id,
                        "decided_at": datetime.now(UTC),
                        "decided_by": "run_release_feature_review_loop",
                        "rationale": rationale,
                        "evidence_paths": evidence_paths,
                        "finding_id": item.finding_id,
                        "classification": item.classification,
                        "selected_action": item.selected_action,
                        "outcome": outcome.value,
                        "fallback_plan": fallback_plan,
                        "validators_to_rerun": ["feature_review_recheck"],
                    }
                )
                attempt_path = write_supervisor_decision_artifact(
                    release_bundle_path=release_root, decision=decision_record
                )
                written[item.finding_id] = attempt_path
                stable_record = decision_record.model_copy(update={"decision_id": stable_decision_id})
                stable_path = write_supervisor_decision_artifact(
                    release_bundle_path=release_root, decision=stable_record
                )
                if item.classification in {"scope_expansion", "backlog_follow_up"} and item.selected_action == "defer":
                    proposal_by_finding_id[item.finding_id] = FeatureReviewProposalRecord.model_validate(
                        {
                            "finding_id": item.finding_id,
                            "classification": item.classification,
                            "selected_action": item.selected_action,
                            "decision_artifact_path": str(stable_path),
                            "matched_previous_finding_id": item.matched_previous_finding_id,
                            "attempt": attempt,
                        }
                    )
                _report(
                    progress,
                    "event=feature_review_non_blocking_finding_classified attempt="
                    + str(attempt)
                    + " finding_id="
                    + item.finding_id
                    + " classification="
                    + item.classification
                    + " action="
                    + item.selected_action
                    + " matched_previous_finding_id="
                    + match_hint
                    + " adjacent_similarity="
                    + f"{item.adjacent_similarity:.3f}",
                )
            return written

        if (
            loop_index >= max_feature_review_repair_loops
            and required_findings
            and decision.recommendation != FeatureReviewRecommendation.ESCALATE
        ):
            _report(
                progress,
                "event=feature_review_convergence_limit_reached "
                f"limit={max_feature_review_repair_loops} unresolved_required_findings="
                + json.dumps([finding.finding_id for finding in required_findings], sort_keys=True),
            )
            rerun_verification(loop_index + 1, decision)
            try:
                convergence = compute_convergence()
            except FeatureReviewClassificationError as error:
                gating_decision = Decision.ESCALATED
                decision = decision.model_copy(
                    update={
                        "reviewer": "deterministic",
                        "summary": f"Feature review finding classification failed: {error}",
                        "recommendation": "escalate",
                    }
                )
                feature_review_decision = decision
                feature_review_path = write_feature_review_decision(release_root, decision)
                unresolved_finding_ids = (
                    [finding.finding_id for finding in decision.findings]
                    or [f"{release_id}:feature_review_classification_failed"]
                )
                unresolved_finding_ids.insert(0, f"{release_id}:schema_invalid_reviewer_output")
                feature_review_recheck = FeatureReviewRecheckRecord(
                    release_id=release_id,
                    unresolved_finding_ids=unresolved_finding_ids,
                    resolved_finding_ids=[],
                    accepted_finding_ids=[],
                    stop_reason="blocked_by_hard_gate",
                )
                feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
                return build_result()

            integration_commit = _git_rev_parse(config.repo_path, integration_branch)
            final_integration_verification_path = _run_final_integration_verification(
                release_id=release_id,
                release_root=release_root,
                repo_path=config.repo_path,
                integration_branch=integration_branch,
                integration_commit=integration_commit,
                commands=integration_verification_commands(),
                timeout_seconds=verification_timeout_seconds,
                progress=progress,
            )
            verification_payload = json.loads(
                final_integration_verification_path.read_text(encoding="utf-8")
            )
            last_verification_ok = bool(verification_payload.get("success"))
            log_path = verification_payload.get("verification_log_path")
            if isinstance(log_path, str) and log_path.strip():
                last_verification_log_path = Path(log_path)

            missing_release_evidence: list[str] = []
            if feature_review_path is None or not feature_review_path.exists():
                missing_release_evidence.append("feature_review.json")
            if final_integration_verification_path is None or not final_integration_verification_path.exists():
                missing_release_evidence.append("final_integration_verification.json")
            if last_verification_log_path is None or not last_verification_log_path.exists():
                missing_release_evidence.append("verification.log")
            if missing_release_evidence:
                _report(
                    progress,
                    "event=final_review_missing_release_evidence artifacts="
                    + json.dumps(sorted(set(missing_release_evidence)), sort_keys=True),
                )
                gating_decision = Decision.ESCALATED
                sentinel_ids = [
                    f"{release_id}:missing_release_evidence:{item}"
                    for item in sorted(set(missing_release_evidence))
                ]
                feature_review_recheck = FeatureReviewRecheckRecord(
                    release_id=release_id,
                    unresolved_finding_ids=sentinel_ids,
                    resolved_finding_ids=[],
                    accepted_finding_ids=[],
                    deferred_finding_ids=[],
                    stop_reason="blocked_by_hard_gate",
                )
                feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
                return build_result()

            accepted_optional_ids, deferred_optional_ids = optional_recheck_ids() if last_verification_ok else ([], [])
            accepted_required_ids = (
                {
                    finding.finding_id
                    for finding in required_findings
                    if _is_verification_only_or_conditional_finding(finding)
                }
                if last_verification_ok
                else set()
            )
            write_required_finding_classifications(
                attempt=loop_index + 1,
                accepted_required_finding_ids=accepted_required_ids,
                write_stable_latest=True,
            )
            unresolved_ids: list[str] = []
            accepted_ids: list[str] = []
            deferred_ids: list[str] = []
            malformed_ids: list[str] = []

            finding_by_id = {finding.finding_id: finding for finding in decision.findings}
            convergence_by_id = {item.finding_id: item for item in convergence.findings}

            for finding_id in sorted(finding_by_id):
                finding = finding_by_id[finding_id]
                item = convergence_by_id.get(finding_id)
                raw_classification = item.classification if item is not None else "blocker"
                raw_action = item.selected_action if item is not None else "repair"

                if finding_id in accepted_required_ids:
                    classification = FinalReviewFindingAdjudicationClassification.VERIFICATION_ONLY
                    selected_action = FinalReviewFindingAdjudicationAction.ACCEPT
                    outcome = FinalReviewFindingAdjudicationOutcome.CONTINUE
                elif raw_classification == "blocker":
                    classification = FinalReviewFindingAdjudicationClassification.BLOCKER
                    selected_action = FinalReviewFindingAdjudicationAction.REPAIR
                    outcome = FinalReviewFindingAdjudicationOutcome.CONTINUE
                elif raw_classification == "soft_finding":
                    classification = FinalReviewFindingAdjudicationClassification.ACCEPTED_RISK
                    selected_action = FinalReviewFindingAdjudicationAction.ACCEPT
                    outcome = FinalReviewFindingAdjudicationOutcome.CONTINUE
                elif raw_classification == "soft_observability":
                    classification = FinalReviewFindingAdjudicationClassification.SOFT_OBSERVABILITY
                    selected_action = FinalReviewFindingAdjudicationAction.ACCEPT
                    outcome = FinalReviewFindingAdjudicationOutcome.CONTINUE
                elif raw_classification == "false_positive":
                    classification = FinalReviewFindingAdjudicationClassification.FALSE_POSITIVE
                    selected_action = FinalReviewFindingAdjudicationAction.ACCEPT
                    outcome = FinalReviewFindingAdjudicationOutcome.CONTINUE
                elif raw_classification == "duplicate":
                    classification = FinalReviewFindingAdjudicationClassification.DUPLICATE
                    selected_action = FinalReviewFindingAdjudicationAction.DEFER
                    outcome = FinalReviewFindingAdjudicationOutcome.STOP_FINDING
                elif raw_classification == "scope_expansion":
                    classification = FinalReviewFindingAdjudicationClassification.SCOPE_EXPANSION
                    selected_action = FinalReviewFindingAdjudicationAction.DEFER
                    outcome = FinalReviewFindingAdjudicationOutcome.STOP_FINDING
                elif raw_classification == "backlog_follow_up":
                    classification = FinalReviewFindingAdjudicationClassification.BACKLOG_FOLLOW_UP
                    selected_action = FinalReviewFindingAdjudicationAction.DEFER
                    outcome = FinalReviewFindingAdjudicationOutcome.STOP_FINDING
                else:
                    classification = FinalReviewFindingAdjudicationClassification.BLOCKER
                    selected_action = FinalReviewFindingAdjudicationAction.REPAIR
                    outcome = FinalReviewFindingAdjudicationOutcome.CONTINUE

                if not last_verification_ok and selected_action in {
                    FinalReviewFindingAdjudicationAction.ACCEPT,
                    FinalReviewFindingAdjudicationAction.DEFER,
                }:
                    classification = FinalReviewFindingAdjudicationClassification.BLOCKER
                    selected_action = FinalReviewFindingAdjudicationAction.REPAIR
                    outcome = FinalReviewFindingAdjudicationOutcome.CONTINUE

                evidence_paths: list[str] = []
                if feature_review_path is not None:
                    evidence_paths.append(str(feature_review_path.resolve()))
                if final_integration_verification_path is not None:
                    evidence_paths.append(str(final_integration_verification_path.resolve()))
                if last_verification_log_path is not None and last_verification_log_path.exists():
                    evidence_paths.append(str(last_verification_log_path.resolve()))

                if (
                    classification
                    in {
                        FinalReviewFindingAdjudicationClassification.ACCEPTED_RISK,
                        FinalReviewFindingAdjudicationClassification.BACKLOG_FOLLOW_UP,
                        FinalReviewFindingAdjudicationClassification.SCOPE_EXPANSION,
                        FinalReviewFindingAdjudicationClassification.DUPLICATE,
                        FinalReviewFindingAdjudicationClassification.FALSE_POSITIVE,
                        FinalReviewFindingAdjudicationClassification.SOFT_OBSERVABILITY,
                        FinalReviewFindingAdjudicationClassification.VERIFICATION_ONLY,
                    }
                    and not evidence_paths
                ):
                    malformed_ids.append(finding_id)
                    classification = FinalReviewFindingAdjudicationClassification.BLOCKER
                    selected_action = FinalReviewFindingAdjudicationAction.REPAIR
                    outcome = FinalReviewFindingAdjudicationOutcome.CONTINUE

                stable_decision_id = f"{release_id}__final_review_finding__{finding_id}"
                attempt_decision_id = f"{stable_decision_id}__attempt_{loop_index + 1}"
                decision_record = FinalReviewFindingAdjudicationDecision.model_validate(
                    {
                        "decision_id": attempt_decision_id,
                        "release_id": release_id,
                        "decided_at": datetime.now(UTC),
                        "decided_by": "run_release_feature_review_loop",
                        "rationale": (
                            "Final adjudication after feature-review convergence limit; "
                            f"classification={classification.value} action={selected_action.value} "
                            f"raw_classification={raw_classification} raw_action={raw_action}."
                        ),
                        "evidence_paths": evidence_paths,
                        "finding_id": finding_id,
                        "classification": classification.value,
                        "selected_action": selected_action.value,
                        "outcome": outcome.value,
                        "fallback_plan": "Stop release finalization and escalate if blockers or malformed reviewer evidence remain.",
                        "validators_to_rerun": integration_verification_commands(),
                    }
                )
                attempt_path = write_supervisor_decision_artifact(
                    release_bundle_path=release_root, decision=decision_record
                )
                stable_record = decision_record.model_copy(update={"decision_id": stable_decision_id})
                stable_path = write_supervisor_decision_artifact(
                    release_bundle_path=release_root, decision=stable_record
                )
                final_review_finding_adjudication_paths.append(stable_path)

                if classification == FinalReviewFindingAdjudicationClassification.BLOCKER:
                    unresolved_ids.append(finding_id)
                elif selected_action == FinalReviewFindingAdjudicationAction.ACCEPT:
                    accepted_ids.append(finding_id)
                else:
                    deferred_ids.append(finding_id)

            if malformed_ids:
                _report(
                    progress,
                    "event=final_review_schema_invalid_reviewer_output finding_ids="
                    + json.dumps(sorted(set(malformed_ids)), sort_keys=True),
                )
                gating_decision = Decision.ESCALATED
                unresolved_payload = sorted(set(unresolved_ids or malformed_ids))
                unresolved_payload.insert(0, f"{release_id}:schema_invalid_reviewer_output")
                feature_review_recheck = FeatureReviewRecheckRecord(
                    release_id=release_id,
                    unresolved_finding_ids=unresolved_payload,
                    resolved_finding_ids=[],
                    accepted_finding_ids=sorted(set(accepted_ids) | set(accepted_optional_ids)),
                    deferred_finding_ids=sorted(set(deferred_ids) | set(deferred_optional_ids)),
                    stop_reason="blocked_by_hard_gate",
                )
                feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
                return build_result()

            if unresolved_ids or not last_verification_ok:
                gating_decision = Decision.NEEDS_REVISION
                feature_review_recheck = FeatureReviewRecheckRecord(
                    release_id=release_id,
                    unresolved_finding_ids=sorted(set(unresolved_ids)),
                    resolved_finding_ids=[],
                    accepted_finding_ids=sorted(set(accepted_ids) | set(accepted_optional_ids)),
                    deferred_finding_ids=sorted(set(deferred_ids) | set(deferred_optional_ids)),
                    stop_reason="blocked_by_retry_budget",
                )
                feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
                return build_result()

            gating_decision = Decision.ACCEPTED
            accepted_risks = list(decision.accepted_risks)
            accepted_risks.append(
                "Reached feature-review convergence limit; ran final integration verification and adjudicated remaining findings as non-blocking."
            )
            decision = decision.model_copy(update={"accepted_risks": accepted_risks})
            feature_review_decision = decision
            feature_review_path = write_feature_review_decision(release_root, decision)
            feature_review_recheck = FeatureReviewRecheckRecord(
                release_id=release_id,
                unresolved_finding_ids=[],
                resolved_finding_ids=[],
                accepted_finding_ids=sorted(set(accepted_ids) | set(accepted_optional_ids)),
                deferred_finding_ids=sorted(set(deferred_ids) | set(deferred_optional_ids)),
                stop_reason="accepted_with_rationale",
            )
            feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
            return build_result()

        write_non_blocking_finding_classifications(attempt=loop_index + 1)

        if decision.recommendation == FeatureReviewRecommendation.ESCALATE:
            write_required_finding_classifications(attempt=loop_index + 1, write_stable_latest=True)
            gating_decision = Decision.ESCALATED
            unresolved_finding_ids = [finding.finding_id for finding in decision.findings]
            if not unresolved_finding_ids and outstanding_required_finding_ids:
                unresolved_finding_ids = sorted(outstanding_required_finding_ids)
            accepted_optional_ids, deferred_optional_ids = optional_recheck_ids()
            feature_review_recheck = FeatureReviewRecheckRecord(
                release_id=release_id,
                unresolved_finding_ids=unresolved_finding_ids or [f"{release_id}:feature_review_blocked"],
                resolved_finding_ids=[],
                accepted_finding_ids=accepted_optional_ids,
                deferred_finding_ids=deferred_optional_ids,
                stop_reason="blocked_by_hard_gate",
                )
            feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
            return build_result()

        if not required_findings:
            stop_reason = "resolved" if not decision.findings else "accepted_with_rationale"
            optional_finding_ids = {finding.finding_id for finding in optional_findings}
            accepted_optional_ids, deferred_optional_ids = optional_recheck_ids()
            if stop_reason == "accepted_with_rationale" and not decision.accepted_risks:
                decision = decision.model_copy(
                    update={
                        "accepted_risks": [
                            (
                                "Accepted optional findings after reviewer re-check with no required repairs; "
                                "follow-up remains non-blocking for release finalization."
                            )
                        ]
                    }
                )
                feature_review_decision = decision
                feature_review_path = write_feature_review_decision(release_root, decision)
            feature_review_recheck = FeatureReviewRecheckRecord(
                release_id=release_id,
                unresolved_finding_ids=[],
                resolved_finding_ids=[
                    finding.finding_id
                    for finding in decision.findings
                    if finding.finding_id not in optional_finding_ids
                ],
                accepted_finding_ids=accepted_optional_ids,
                deferred_finding_ids=deferred_optional_ids,
                stop_reason=stop_reason,
            )
            feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
            return build_result()

        if loop_index >= max_feature_review_repair_loops:
            verification_ok = last_verification_ok
            adjudicated_finding_ids = (
                [
                    finding.finding_id
                    for finding in required_findings
                    if _is_verification_only_or_conditional_finding(finding)
                ]
                if verification_ok
                else []
            )
            if adjudicated_finding_ids and len(adjudicated_finding_ids) == len(required_findings):
                write_required_finding_classifications(
                    attempt=loop_index + 1,
                    accepted_required_finding_ids=set(adjudicated_finding_ids),
                )
                accepted_risks = list(decision.accepted_risks)
                accepted_risks.append(
                    "Accepted required feature-review finding(s) after retry budget because "
                    "they were verification-only or conditional repair findings and the configured "
                    "integration verification rerun passed."
                )
                decision = decision.model_copy(update={"accepted_risks": accepted_risks})
                feature_review_decision = decision
                feature_review_path = write_feature_review_decision(release_root, decision)
                feature_review_recheck = FeatureReviewRecheckRecord(
                    release_id=release_id,
                    unresolved_finding_ids=[],
                    resolved_finding_ids=[],
                    accepted_finding_ids=sorted(adjudicated_finding_ids),
                    stop_reason="accepted_with_rationale",
                )
                feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
                return build_result()
            write_required_finding_classifications(attempt=loop_index + 1)
            gating_decision = Decision.NEEDS_REVISION
            accepted_optional_ids, deferred_optional_ids = optional_recheck_ids()
            feature_review_recheck = FeatureReviewRecheckRecord(
                release_id=release_id,
                unresolved_finding_ids=[finding.finding_id for finding in required_findings],
                resolved_finding_ids=[],
                accepted_finding_ids=accepted_optional_ids,
                deferred_finding_ids=deferred_optional_ids,
                stop_reason="blocked_by_retry_budget",
            )
            feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
            return build_result()

        write_required_finding_classifications(attempt=loop_index + 1, write_stable_latest=False)

        repair_blocker_ids = set(convergence.blocking_finding_ids)
        if not repair_blocker_ids:
            gating_decision = Decision.ACCEPTED
            write_required_finding_classifications(attempt=loop_index + 1, write_stable_latest=True)
            accepted_optional_ids, deferred_optional_ids = optional_recheck_ids()
            feature_review_recheck = FeatureReviewRecheckRecord(
                release_id=release_id,
                unresolved_finding_ids=[],
                resolved_finding_ids=[],
                accepted_finding_ids=accepted_optional_ids,
                deferred_finding_ids=deferred_optional_ids,
                stop_reason="accepted_with_rationale",
            )
            feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
            return build_result()

        repair_decision = decision.model_copy(
            update={
                "findings": [
                    finding
                    for finding in decision.findings
                    if finding.finding_id in repair_blocker_ids and finding.required_repairs
                ]
            }
        )

        generated = generate_repair_contracts_for_required_findings(
            decision=repair_decision,
            source_contracts=source_contracts,
        )
        if not generated:
            gating_decision = Decision.NEEDS_REVISION
            accepted_optional_ids, deferred_optional_ids = optional_recheck_ids()
            feature_review_recheck = FeatureReviewRecheckRecord(
                release_id=release_id,
                unresolved_finding_ids=[finding.finding_id for finding in required_findings],
                resolved_finding_ids=[],
                accepted_finding_ids=accepted_optional_ids,
                deferred_finding_ids=deferred_optional_ids,
                stop_reason="blocked_by_hard_gate",
            )
            feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
            return FeatureReviewLoopResult(
                task_results=all_task_results,
                feature_review_path=feature_review_path,
                feature_review_recheck_path=feature_review_recheck_path,
                feature_review_decision=feature_review_decision,
                feature_review_recheck=feature_review_recheck,
                feature_review_proposals=current_proposals(),
                gating_decision=gating_decision,
            )

        repair_dir = output_root / f"repairs_{loop_index + 1:02d}"
        repair_dir.mkdir(parents=True, exist_ok=True)
        _report(progress, f"event=feature_review_repairs_started attempt={loop_index + 1} repairs={len(generated)}")
        for contract in generated:
            contract_path = repair_dir / f"{contract.task_id}.yaml"
            _write_contract_yaml(contract_path, contract.suggested_contract)
            result = _run_one_release_task(
                project_id=project_id,
                config_repo_path=config.repo_path,
                config_dir=config_dir,
                runs_dir=runs_dir,
                task_base_branch=integration_branch,
                contract_path=contract_path,
                task=contract.suggested_contract,
                executor=executor,
                verification_timeout_seconds=verification_timeout_seconds,
                allow_dirty=allow_dirty,
                commit_on_accept=commit_on_accept,
                merge_on_accept=merge_on_accept,
                push_on_accept=push_on_accept,
                debug_keep_artifacts=debug_keep_artifacts,
                progress=progress,
            )
            all_task_results.append(result)
            if result.decision.decision != Decision.ACCEPTED:
                gating_decision = Decision.NEEDS_REVISION
                accepted_optional_ids, deferred_optional_ids = optional_recheck_ids()
                feature_review_recheck = FeatureReviewRecheckRecord(
                    release_id=release_id,
                    unresolved_finding_ids=[finding.finding_id for finding in required_findings],
                    resolved_finding_ids=[],
                    accepted_finding_ids=accepted_optional_ids,
                    deferred_finding_ids=deferred_optional_ids,
                    stop_reason="blocked_by_hard_gate",
                )
                feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
                return build_result()

        verification_ok = rerun_verification(loop_index + 1, decision)
        if not verification_ok:
            gating_decision = Decision.NEEDS_REVISION
            accepted_optional_ids, deferred_optional_ids = optional_recheck_ids()
            feature_review_recheck = FeatureReviewRecheckRecord(
                release_id=release_id,
                unresolved_finding_ids=[finding.finding_id for finding in required_findings],
                resolved_finding_ids=[],
                accepted_finding_ids=accepted_optional_ids,
                deferred_finding_ids=deferred_optional_ids,
                stop_reason="blocked_by_hard_gate",
            )
            feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
            return build_result()
        write_required_finding_classifications(attempt=loop_index + 1, write_stable_latest=True)
        decision = run_review(attempt=loop_index + 2)
        previous_review_decisions.append(decision)

    gating_decision = Decision.NEEDS_REVISION
    feature_review_recheck = FeatureReviewRecheckRecord(
        release_id=release_id,
        unresolved_finding_ids=[finding.finding_id for finding in decision.findings],
        resolved_finding_ids=[],
        accepted_finding_ids=[],
        stop_reason="blocked_by_retry_budget",
    )
    feature_review_recheck_path = write_feature_review_recheck(release_root, feature_review_recheck)
    return build_result()


def _normalize_feature_review_model_output_if_needed(
    *,
    release_id: str,
    release_root: Path,
    context,
    backend,
    fallback_decision: FeatureReviewDecision,
    progress: Callable[[str], None] | None,
) -> tuple[FeatureReviewDecision, Path | None, Path | None]:
    if not fallback_decision.findings:
        return fallback_decision, None, None
    blocked_summary = fallback_decision.findings[0].summary.lower()
    if "not valid featurereviewdecision json" not in blocked_summary:
        return fallback_decision, None, None

    raw_paths = [
        path
        for path in (
            getattr(backend, "stdout_path", None),
            getattr(backend, "stderr_path", None),
            getattr(backend, "metadata_path", None),
            getattr(backend, "prompt_path", None),
        )
        if path is not None
    ]
    raw_payload = _extract_json_object_from_text(getattr(backend, "raw_output", ""))
    if raw_payload is None:
        decision_path = _write_feature_review_output_normalization_decision(
            release_id=release_id,
            release_root=release_root,
            raw_paths=raw_paths,
            validation_errors=[],
            selected_action=ModelOutputNormalizationAction.REFUSE,
            outcome=ModelOutputNormalizationOutcome.REFUSED_AND_STOP,
            normalized_artifact_path=None,
            refusal_reason="Reviewer output was not parseable JSON; bounded normalization could not proceed.",
        )
        _report(progress, "event=feature_review_output_normalization_refused reason=unparseable_json")
        return fallback_decision, decision_path, None

    validation_errors = _feature_review_validation_errors(raw_payload)
    normalized_payload, refusal_reason = _bounded_normalize_feature_review_payload(
        raw_payload=raw_payload,
        context=context,
    )
    if normalized_payload is None:
        decision_path = _write_feature_review_output_normalization_decision(
            release_id=release_id,
            release_root=release_root,
            raw_paths=raw_paths,
            validation_errors=validation_errors,
            selected_action=ModelOutputNormalizationAction.REFUSE,
            outcome=ModelOutputNormalizationOutcome.REFUSED_AND_STOP,
            normalized_artifact_path=None,
            refusal_reason=refusal_reason or "Reviewer output normalization refused by bounded policy.",
        )
        refusal_text = (refusal_reason or "").lower()
        if "missing required release evidence artifacts" in refusal_text:
            _report(progress, "event=feature_review_output_normalization_refused reason=missing_release_evidence")
        else:
            _report(progress, "event=feature_review_output_normalization_refused reason=schema_invalid_reviewer_output")
        return fallback_decision, decision_path, None

    try:
        normalized_decision = FeatureReviewDecision.model_validate(normalized_payload)
        _validate_feature_review_review_decision_bridge(normalized_decision)
    except Exception as error:  # noqa: BLE001 - refusal is captured in decision artifact.
        decision_path = _write_feature_review_output_normalization_decision(
            release_id=release_id,
            release_root=release_root,
            raw_paths=raw_paths,
            validation_errors=validation_errors,
            selected_action=ModelOutputNormalizationAction.REFUSE,
            outcome=ModelOutputNormalizationOutcome.REFUSED_AND_STOP,
            normalized_artifact_path=None,
            refusal_reason=f"Normalized reviewer output remained invalid: {error}",
        )
        _report(progress, "event=feature_review_output_normalization_refused reason=invalid_after_normalization")
        return fallback_decision, decision_path, None

    normalized_artifact_path = release_root / "feature_review" / "normalized_feature_review_decision.json"
    normalized_artifact_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_artifact_path.write_text(
        json.dumps(normalized_decision.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    decision_path = _write_feature_review_output_normalization_decision(
        release_id=release_id,
        release_root=release_root,
        raw_paths=raw_paths,
        validation_errors=validation_errors,
        selected_action=ModelOutputNormalizationAction.APPLY_NORMALIZATION,
        outcome=ModelOutputNormalizationOutcome.NORMALIZED_AND_RETRY,
        normalized_artifact_path=normalized_artifact_path,
        refusal_reason=None,
    )
    _report(progress, f"event=feature_review_output_normalized path={normalized_artifact_path}")
    return normalized_decision, decision_path, normalized_artifact_path


def _extract_json_object_from_text(text: str) -> dict[str, object] | None:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        loaded = None
    if isinstance(loaded, dict):
        return loaded
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : index + 1]
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def _feature_review_validation_errors(candidate: object) -> list[dict[str, str]]:
    try:
        FeatureReviewDecision.model_validate(candidate)
        return []
    except Exception as error:  # noqa: BLE001 - validation errors are best-effort evidence.
        errors_fn = getattr(error, "errors", None)
        if not callable(errors_fn):
            return []
        payload: list[dict[str, str]] = []
        for item in errors_fn():
            loc = item.get("loc", ())
            payload.append(
                {
                    "field": ".".join(str(token) for token in loc) if loc else "<root>",
                    "message": str(item.get("msg", "validation failed")),
                    "error_type": str(item.get("type", "value_error")),
                }
            )
        return payload


def _bounded_normalize_feature_review_payload(
    *,
    raw_payload: dict[str, object],
    context,
) -> tuple[dict[str, object] | None, str | None]:
    candidate = dict(raw_payload)
    nested_candidate = None
    for key in ("decision", "feature_review", "review", "result", "output"):
        value = raw_payload.get(key)
        if isinstance(value, dict):
            nested_candidate = value
            break
    if nested_candidate is not None:
        if _payload_looks_like_feature_review(candidate) and _payload_looks_like_feature_review(nested_candidate):
            if _feature_review_semantics_fingerprint(candidate) != _feature_review_semantics_fingerprint(nested_candidate):
                return None, "Reviewer wrapper and nested decision disagree on finding semantics."
        candidate = dict(nested_candidate)

    if not _payload_looks_like_feature_review(candidate):
        return None, "Reviewer output did not include a recognizable FeatureReviewDecision payload."

    normalized = dict(candidate)
    recommendation = str(normalized.get("recommendation", "")).strip().lower()
    recommendation_aliases = {
        "approved": "approve",
        "approve_with_fixes": "approve_with_repairs",
        "requires_repairs": "require_repairs",
        "needs_repairs": "require_repairs",
        "require_changes": "require_repairs",
        "escalated": "escalate",
    }
    if recommendation in recommendation_aliases:
        normalized["recommendation"] = recommendation_aliases[recommendation]
    reviewer = str(normalized.get("reviewer", "")).strip().lower()
    reviewer_aliases = {"model": "strong_model", "strong": "strong_model", "human_reviewer": "human"}
    if reviewer in reviewer_aliases:
        normalized["reviewer"] = reviewer_aliases[reviewer]

    findings = normalized.get("findings")
    if not isinstance(findings, list):
        return None, "Reviewer payload findings were not a list."
    normalized_findings: list[dict[str, object]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            return None, "Reviewer payload contained non-object findings."
        finding_payload = dict(finding)
        if "evidence_paths" not in finding_payload and isinstance(finding_payload.get("evidence"), list):
            finding_payload["evidence_paths"] = finding_payload["evidence"]
        evidence = finding_payload.get("evidence_paths")
        if isinstance(evidence, list):
            cleaned = [str(item).strip() for item in evidence if str(item).strip()]
        else:
            cleaned = []
        if not cleaned:
            derived = _derive_feature_review_evidence_paths(finding_payload=finding_payload, context=context)
            if not derived:
                finding_id = str(finding_payload.get("finding_id", "<unknown>"))
                missing_release_artifacts: list[str] = []
                for label, candidate in (
                    ("release_summary_path", getattr(context, "release_summary_path", None)),
                    ("release_review_path", getattr(context, "release_review_path", None)),
                    ("release_metrics_path", getattr(context, "release_metrics_path", None)),
                    ("release_budget_path", getattr(context, "release_budget_path", None)),
                    ("release_tuning_path", getattr(context, "release_tuning_path", None)),
                    ("final_integration_verification_path", getattr(context, "final_integration_verification_path", None)),
                    (
                        "final_integration_verification_log_path",
                        getattr(context, "final_integration_verification_log_path", None),
                    ),
                ):
                    if candidate is None:
                        missing_release_artifacts.append(label)
                        continue
                    try:
                        exists = bool(getattr(candidate, "exists", None) and candidate.exists())
                    except Exception:  # noqa: BLE001 - release evidence must be treated conservatively.
                        exists = False
                    if not exists:
                        missing_release_artifacts.append(label)
                missing_hint = (
                    "; missing_release_evidence_artifacts=" + ", ".join(sorted(set(missing_release_artifacts)))
                    if missing_release_artifacts
                    else ""
                )
                return (
                    None,
                    "missing required release evidence artifacts to derive reviewer finding evidence_paths "
                    f"for finding {finding_id}{missing_hint}",
                )
            finding_payload["evidence_paths"] = derived
        normalized_findings.append(finding_payload)
    normalized_findings.extend(_normalize_feature_review_limitations(payload=normalized, context=context))
    if any(_is_missing_required_final_verification_evidence_finding(item) for item in normalized_findings):
        normalized["recommendation"] = FeatureReviewRecommendation.ESCALATE.value
    normalized["findings"] = normalized_findings
    normalized.pop("limitations", None)
    return normalized, None


def _normalize_feature_review_limitations(*, payload: dict[str, object], context) -> list[dict[str, object]]:
    raw_limitations = payload.get("limitations")
    if not isinstance(raw_limitations, list):
        return []
    normalized: list[dict[str, object]] = []
    for index, entry in enumerate(raw_limitations, start=1):
        limitation = _build_feature_review_limitation_finding(entry=entry, index=index, context=context)
        if limitation is not None:
            normalized.append(limitation)
    return normalized


def _build_feature_review_limitation_finding(*, entry: object, index: int, context) -> dict[str, object] | None:
    text = ""
    raw_evidence: object = None
    kind = ""
    if isinstance(entry, str):
        text = entry.strip()
    elif isinstance(entry, dict):
        text = str(entry.get("summary") or entry.get("detail") or entry.get("message") or "").strip()
        raw_evidence = entry.get("evidence_paths")
        if raw_evidence is None:
            raw_evidence = entry.get("evidence")
        kind = str(entry.get("type") or entry.get("kind") or "").strip().lower()
    else:
        return None
    if not text:
        return None

    lower = f"{kind} {text.lower()}"
    if "truncat" in lower:
        limitation_kind = "truncated_context"
    elif "uncertain" in lower or "unsure" in lower or "confidence" in lower:
        limitation_kind = "uncertainty"
    elif "missing" in lower and "evidence" in lower:
        limitation_kind = "missing_evidence_reference"
    else:
        limitation_kind = "reviewer_limitation"

    required_repairs: list[str] = []
    optional_follow_ups: list[str] = []
    severity = "moderate"
    if _text_requires_final_verification_evidence(lower):
        severity = "high"
        required_repairs.append(
            "Provide required final integration verification evidence paths in the reviewer decision payload."
        )
    else:
        optional_follow_ups.append(
            "Preserve this reviewer limitation in handoff artifacts and provide missing context/evidence in the next review pass."
        )

    evidence_paths = _clean_feature_review_evidence_list(raw_evidence)
    if not evidence_paths:
        evidence_paths = _default_feature_review_limitation_evidence_paths(context=context)
    if not evidence_paths:
        return None

    return {
        "finding_id": f"limitation-{limitation_kind}-{index}",
        "severity": severity,
        "summary": f"Reviewer limitation ({limitation_kind}): {text}",
        "affected_files": ["feature_review_context"],
        "evidence_paths": evidence_paths,
        "required_repairs": required_repairs,
        "optional_follow_ups": optional_follow_ups,
    }


def _clean_feature_review_evidence_list(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    cleaned: list[str] = []
    for item in values:
        value = str(item).strip()
        if value:
            cleaned.append(value)
    return cleaned


def _default_feature_review_limitation_evidence_paths(*, context) -> list[str]:
    candidates = [
        context.final_integration_verification_path,
        context.final_integration_verification_log_path,
        context.final_integration_worktree_log_path,
        context.release_summary_path,
        context.release_review_path,
    ]
    resolved = [str(path.resolve()) for path in candidates if path is not None]
    if resolved:
        return resolved
    return [str(path).strip() for path in getattr(context, "changed_files", []) if str(path).strip()]


def _text_requires_final_verification_evidence(text: str) -> bool:
    mentions_final_verification = (
        "final integration verification" in text
        or ("final verification" in text and "integration" in text)
    )
    if not mentions_final_verification:
        return False
    has_required_marker = any(marker in text for marker in ("required", "must", "cannot", "hard stop"))
    mentions_missing_evidence = "missing" in text and "evidence" in text
    return has_required_marker and mentions_missing_evidence


def _is_missing_required_final_verification_evidence_finding(finding: dict[str, object]) -> bool:
    summary = str(finding.get("summary", "")).lower()
    repairs = " ".join(str(item).lower() for item in finding.get("required_repairs", []) if isinstance(item, str))
    text = f"{summary} {repairs}"
    return _text_requires_final_verification_evidence(text)


def _payload_looks_like_feature_review(payload: dict[str, object]) -> bool:
    return "recommendation" in payload and "findings" in payload and "summary" in payload


def _feature_review_semantics_fingerprint(payload: dict[str, object]) -> dict[str, object]:
    findings = payload.get("findings")
    normalized_findings: list[dict[str, object]] = []
    if isinstance(findings, list):
        for item in findings:
            if not isinstance(item, dict):
                continue
            normalized_findings.append(
                {
                    "finding_id": item.get("finding_id"),
                    "severity": item.get("severity"),
                    "summary": item.get("summary"),
                    "affected_files": item.get("affected_files"),
                    "required_repairs": item.get("required_repairs"),
                    "optional_follow_ups": item.get("optional_follow_ups"),
                }
            )
    return {
        "reviewer": payload.get("reviewer"),
        "summary": payload.get("summary"),
        "recommendation": payload.get("recommendation"),
        "findings": normalized_findings,
    }


def _derive_feature_review_evidence_paths(*, finding_payload: dict[str, object], context) -> list[str]:
    derivable: list[str] = []
    affected_files = finding_payload.get("affected_files")
    if isinstance(affected_files, list):
        changed = {path for path in context.changed_files}
        for value in affected_files:
            path = str(value).strip()
            if path and path in changed:
                derivable.append(path)
            if path and path.startswith("runs/"):
                derivable.append(path)

    artifact_paths = [
        context.release_summary_path,
        context.release_review_path,
        context.release_metrics_path,
        context.release_budget_path,
        context.release_tuning_path,
    ]
    summary_text = " ".join(
        str(value).lower()
        for value in [
            finding_payload.get("summary", ""),
            " ".join(str(item) for item in finding_payload.get("required_repairs", []) if isinstance(item, str)),
            " ".join(str(item) for item in finding_payload.get("optional_follow_ups", []) if isinstance(item, str)),
        ]
    )
    artifact_keywords = {
        "summary": "release_summary.json",
        "review": "release_review.md",
        "metric": "release_metrics.json",
        "budget": "release_budget.json",
        "tuning": "release_tuning.md",
    }
    for artifact_path in artifact_paths:
        if artifact_path is None:
            continue
        filename = artifact_path.name.lower()
        if filename in summary_text:
            derivable.append(str(artifact_path.resolve()))
            continue
        if any(keyword in summary_text and target == filename for keyword, target in artifact_keywords.items()):
            derivable.append(str(artifact_path.resolve()))

    unique: list[str] = []
    seen: set[str] = set()
    for value in derivable:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def _validate_feature_review_review_decision_bridge(decision: FeatureReviewDecision) -> None:
    mapped_decision = {
        FeatureReviewRecommendation.APPROVE: Decision.ACCEPTED,
        FeatureReviewRecommendation.APPROVE_WITH_REPAIRS: Decision.NEEDS_REVISION,
        FeatureReviewRecommendation.REQUIRE_REPAIRS: Decision.NEEDS_REVISION,
        FeatureReviewRecommendation.ESCALATE: Decision.ESCALATED,
    }[decision.recommendation]
    ReviewDecision.model_validate(
        {
            "task_id": f"feature-review:{decision.release_id}",
            "decision": mapped_decision.value,
            "reviewer": decision.reviewer.value,
            "rationale": decision.summary,
            "risks": list(decision.accepted_risks),
            "follow_up_tasks": [],
            "soft_gate_findings": [],
        }
    )


def _write_feature_review_output_normalization_decision(
    *,
    release_id: str,
    release_root: Path,
    raw_paths: list[Path],
    validation_errors: list[dict[str, str]],
    selected_action: ModelOutputNormalizationAction,
    outcome: ModelOutputNormalizationOutcome,
    normalized_artifact_path: Path | None,
    refusal_reason: str | None,
) -> Path:
    decision = ModelOutputNormalizationDecision.model_validate(
        {
            "decision_type": SupervisorDecisionType.MODEL_OUTPUT_NORMALIZATION,
            "decision_id": f"{release_id}__feature_review_output",
            "release_id": release_id,
            "decided_at": datetime.now(UTC),
            "decided_by": "run_release_feature_review_loop",
            "rationale": (
                "Applied bounded normalization to reviewer output and reran strict feature-review validators."
                if outcome == ModelOutputNormalizationOutcome.NORMALIZED_AND_RETRY
                else "Refused reviewer output normalization because bounded policy could not guarantee safe semantics."
            ),
            "evidence_paths": [str(path.resolve()) for path in raw_paths if path.exists()],
            "risk_level": DecisionRiskLevel.MODERATE,
            "raw_artifact_paths": [str(path.resolve()) for path in raw_paths if path.exists()],
            "validation_errors": [
                ModelOutputValidationError.model_validate(item).model_dump(mode="json")
                for item in validation_errors
            ],
            "selected_action": selected_action.value,
            "outcome": outcome.value,
            "fallback_plan": "Keep deterministic blocked feature-review decision and require bounded reviewer rerun.",
            "validators_to_rerun": ["FeatureReviewDecision", "ReviewDecision"],
            "normalized_artifact_path": str(normalized_artifact_path.resolve()) if normalized_artifact_path else None,
            "refusal_reason": refusal_reason,
        }
    )
    return write_supervisor_decision_artifact(release_bundle_path=release_root, decision=decision)


def _is_verification_only_or_conditional_finding(finding: object) -> bool:
    summary = str(getattr(finding, "summary", "")).lower()
    repairs = " ".join(str(item) for item in getattr(finding, "required_repairs", [])).lower()
    text = f"{summary} {repairs}"
    verification_markers = (
        "verify",
        "verification",
        "confirm",
        "rerun",
        "compileall",
        "pytest",
        "parses",
        "syntax",
        "if needed",
    )
    if not any(marker in text for marker in verification_markers):
        return False
    unconditional_change_markers = (
        "implement ",
        "add ",
        "change ",
        "modify ",
        "remove ",
        "restore ",
        "replace ",
        "repair the source",
        "fix behavior",
    )
    return not any(marker in text for marker in unconditional_change_markers)


def _write_contract_yaml(path: Path, contract: TaskContract) -> None:
    import yaml

    path.write_text(yaml.safe_dump(contract.model_dump(mode="json"), sort_keys=False), encoding="utf-8")


def _run_integration_verification_rerun(
    *,
    repo_path: Path,
    integration_branch: str,
    worktree_path: Path,
    commands: list[str],
    timeout_seconds: int,
    log_path: Path,
    progress: Callable[[str], None] | None,
) -> bool:
    repo_path = repo_path.resolve()
    worktree_path = worktree_path.resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []
    cleanup_root = log_path.parent.resolve()
    _assert_safe_feature_review_rerun_worktree(worktree_path, cleanup_root)
    log_lines.append(f"rerun worktree cleanup guard: {worktree_path} is under {cleanup_root}")

    if worktree_path.exists():
        run_process(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_path,
            timeout_seconds=120,
        )

    add = run_process(
        ["git", "worktree", "add", "--detach", str(worktree_path), integration_branch],
        cwd=repo_path,
        timeout_seconds=120,
    )
    log_lines.append(f"$ git worktree add --detach {worktree_path} {integration_branch}")
    log_lines.append(add.stdout.rstrip())
    log_lines.append(add.stderr.rstrip())
    if add.exit_code != 0:
        log_path.write_text("\n".join(line for line in log_lines if line) + "\n", encoding="utf-8")
        _report(progress, f"event=feature_review_verification_worktree_failed error={add.stderr.strip() or add.stdout.strip()}")
        return False

    ok = True
    for command in commands:
        parts = _command_with_env_prefixes(shlex.split(command))
        log_lines.append(f"$ {command}")
        result = run_process(
            parts,
            cwd=worktree_path,
            timeout_seconds=timeout_seconds,
        )
        log_lines.append(result.stdout.rstrip())
        log_lines.append(result.stderr.rstrip())
        if result.exit_code != 0:
            ok = False
            _report(progress, f"event=feature_review_verification_failed command={json.dumps(command)} exit_code={result.exit_code}")
            break

    remove = run_process(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=repo_path,
        timeout_seconds=120,
    )
    log_lines.append(f"$ git worktree remove --force {worktree_path}")
    log_lines.append(remove.stdout.rstrip())
    log_lines.append(remove.stderr.rstrip())
    if remove.exit_code != 0:
        _report(progress, f"event=feature_review_verification_worktree_cleanup_failed error={remove.stderr.strip() or remove.stdout.strip()}")

    log_path.write_text("\n".join(line for line in log_lines if line) + "\n", encoding="utf-8")
    _report(progress, f"event=feature_review_verification_completed ok={str(ok).lower()} log={log_path}")
    return ok


def _assert_safe_feature_review_rerun_worktree(worktree_path: Path, cleanup_root: Path) -> None:
    if worktree_path == cleanup_root or cleanup_root in worktree_path.parents:
        return
    raise ValueError(
        "feature-review verification rerun worktree must be inside the rerun output directory "
        f"before forced cleanup is allowed: worktree={worktree_path} cleanup_root={cleanup_root}"
    )


def _run_final_integration_verification(
    *,
    release_id: str,
    release_root: Path,
    repo_path: Path,
    integration_branch: str,
    integration_commit: str,
    commands: list[str],
    timeout_seconds: int,
    progress: Callable[[str], None] | None,
) -> Path:
    output_dir = release_root / "final_integration_verification"
    output_dir.mkdir(parents=True, exist_ok=True)
    worktree_path = output_dir / "worktree"
    worktree_log_path = output_dir / "worktree.log"
    _assert_safe_final_integration_verification_worktree(worktree_path.resolve(), output_dir.resolve())
    worktree_log_lines = [f"integration_branch={integration_branch}", f"integration_commit={integration_commit}"]

    if worktree_path.exists():
        worktree_log_lines.append(f"refusing to remove pre-existing final verification worktree: {worktree_path}")
        worktree_log_path.write_text(
            "\n".join(line for line in worktree_log_lines if line) + "\n",
            encoding="utf-8",
        )
        raise ValueError(
            "final integration verification worktree already exists; refusing forced cleanup "
            f"before this run successfully added it: {worktree_path}"
        )
    added_worktree = False
    add = run_process(
        ["git", "worktree", "add", "--detach", str(worktree_path), integration_commit],
        cwd=repo_path,
        timeout_seconds=120,
    )
    worktree_log_lines.append(f"$ git worktree add --detach {worktree_path} {integration_commit}")
    worktree_log_lines.append(add.stdout.rstrip())
    worktree_log_lines.append(add.stderr.rstrip())
    if add.exit_code != 0:
        worktree_log_path.write_text(
            "\n".join(line for line in worktree_log_lines if line) + "\n",
            encoding="utf-8",
        )
        raise ValueError(
            "failed to create final integration verification worktree: "
            + (add.stderr.strip() or add.stdout.strip())
        )
    added_worktree = True

    runner = VerificationRunner(timeout_seconds=timeout_seconds)
    try:
        command_results = runner.run(
            commands=commands,
            worktree_path=worktree_path,
            output_dir=output_dir,
            stop_on_failure=True,
        )
    finally:
        if added_worktree:
            remove = run_process(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=repo_path,
                timeout_seconds=120,
            )
            worktree_log_lines.append(f"$ git worktree remove --force {worktree_path}")
            worktree_log_lines.append(remove.stdout.rstrip())
            worktree_log_lines.append(remove.stderr.rstrip())
            if remove.exit_code != 0:
                _report(
                    progress,
                    "event=final_integration_verification_worktree_cleanup_failed error="
                    + (remove.stderr.strip() or remove.stdout.strip()),
                )
        worktree_log_path.write_text(
            "\n".join(line for line in worktree_log_lines if line) + "\n",
            encoding="utf-8",
        )

    success = bool(command_results) and all(result.exit_code == 0 for result in command_results)
    evidence = FinalIntegrationVerificationEvidence(
        release_id=release_id,
        integration_branch=integration_branch,
        integration_commit=integration_commit,
        verification_log_path=output_dir / "verification.log",
        worktree_log_path=worktree_log_path,
        command_results=command_results,
        success=success,
        verified_at=datetime.now(UTC),
    )
    evidence_path = write_final_integration_verification_evidence(release_root, evidence)
    _report(
        progress,
        "event=final_integration_verification_completed "
        + f"success={str(success).lower()} path={evidence_path}",
    )
    return evidence_path


def _assert_safe_final_integration_verification_worktree(worktree_path: Path, output_dir: Path) -> None:
    expected = (output_dir / "worktree").resolve()
    if worktree_path.resolve() == expected and output_dir.resolve() in worktree_path.resolve().parents:
        return
    raise ValueError(
        "final integration verification worktree must be exactly under the release "
        f"final_integration_verification directory: worktree={worktree_path} output_dir={output_dir}"
    )


def _command_with_env_prefixes(parts: list[str]) -> list[str]:
    env_parts: list[str] = []
    command_parts = list(parts)
    while command_parts and _looks_like_env_assignment(command_parts[0]):
        env_parts.append(command_parts.pop(0))
    if not env_parts:
        return parts
    if not command_parts:
        return parts
    return ["/usr/bin/env", *env_parts, *command_parts]


def _looks_like_env_assignment(value: str) -> bool:
    if "=" not in value:
        return False
    name, _value = value.split("=", 1)
    return bool(name) and all(character == "_" or character.isalnum() for character in name)


def _load_release_plan(config_repo_path: Path, repo_state_path: Path | None) -> ReleasePlan | None:
    if repo_state_path is None:
        return None
    root = repo_state_path if repo_state_path.is_absolute() else config_repo_path / repo_state_path
    path = root / "release_plan.yaml"
    if not path.exists():
        return None
    return load_yaml_model(path, ReleasePlan)


def _finalize_release(
    *,
    release_root: Path,
    release_id: str,
    run_id: str,
    repo_path: Path,
    integration_branch: str,
    base_branch: str,
    integration_commit: str,
    policy: ReleaseFinalizationPolicy | None,
    decision: Decision,
    allowed: bool,
    blocked_reason: str,
    mode: str,
    progress: Callable[[str], None] | None,
) -> tuple[Path | None, FinalizeResult | None]:
    decision_path = release_root / "finalization_decision.json"
    git_commands: list[str] = []
    handoff_path: Path | None = None

    def _write(payload: dict[str, object]) -> Path:
        decision_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return decision_path

    if mode == "none":
        payload = {
            "release_id": release_id,
            "run_id": run_id,
            "requested_mode": mode,
            "policy": policy.model_dump(mode="json") if policy is not None else None,
            "policy_source": "config" if policy is not None else "none",
            "gate": {"allowed": bool(allowed), "reason": blocked_reason, "decision": str(decision)},
            "outcome": "skipped",
            "stop_reason": None,
            "missing_credentials": [],
            "git_commands": git_commands,
            "handoff_path": None,
            "finalization": None,
        }
        return _write(payload), None

    if not allowed:
        _report(progress, f"release_finalization_skipped reason={blocked_reason}")
        payload = {
            "release_id": release_id,
            "run_id": run_id,
            "requested_mode": mode,
            "policy": policy.model_dump(mode="json") if policy is not None else None,
            "policy_source": "config" if policy is not None else "missing",
            "gate": {"allowed": bool(allowed), "reason": blocked_reason, "decision": str(decision)},
            "outcome": "stopped",
            "stop_reason": "failed_gate",
            "missing_credentials": [],
            "git_commands": git_commands,
            "handoff_path": None,
            "finalization": None,
        }
        return _write(payload), None

    if decision != Decision.ACCEPTED:
        _report(progress, f"release_finalization_skipped decision={decision}")
        payload = {
            "release_id": release_id,
            "run_id": run_id,
            "requested_mode": mode,
            "policy": policy.model_dump(mode="json") if policy is not None else None,
            "policy_source": "config" if policy is not None else "missing",
            "gate": {"allowed": bool(allowed), "reason": blocked_reason, "decision": str(decision)},
            "outcome": "stopped",
            "stop_reason": "release_decision_not_accepted",
            "missing_credentials": [],
            "git_commands": git_commands,
            "handoff_path": None,
            "finalization": None,
        }
        return _write(payload), None

    if policy is None:
        _report(progress, "event=release_finalization_stopped reason=missing_policy")
        payload = {
            "release_id": release_id,
            "run_id": run_id,
            "requested_mode": mode,
            "policy": None,
            "policy_source": "missing",
            "gate": {"allowed": bool(allowed), "reason": blocked_reason, "decision": str(decision)},
            "outcome": "stopped",
            "stop_reason": "missing_policy",
            "missing_credentials": [],
            "git_commands": git_commands,
            "handoff_path": None,
            "finalization": None,
        }
        return _write(payload), None

    missing_credentials = sorted(
        env for env in policy.required_credential_env_vars if not str(os.environ.get(env, "")).strip()
    )
    if missing_credentials:
        _report(
            progress,
            "event=release_finalization_stopped reason=missing_credentials vars="
            + json.dumps(missing_credentials, sort_keys=True),
        )
        payload = {
            "release_id": release_id,
            "run_id": run_id,
            "requested_mode": mode,
            "policy": policy.model_dump(mode="json"),
            "policy_source": "config",
            "gate": {"allowed": bool(allowed), "reason": blocked_reason, "decision": str(decision)},
            "outcome": "stopped",
            "stop_reason": "missing_credentials",
            "missing_credentials": missing_credentials,
            "git_commands": git_commands,
            "handoff_path": None,
            "finalization": None,
        }
        return _write(payload), None

    try:
        if policy.policy == ReleaseFinalizationPolicyName.PUSH_FEATURE:
            git_commands.append(f"git push origin {integration_branch}")
            push_branch(repo_path, integration_branch)
            _report(progress, f"event=release_pushed branch=origin/{integration_branch}")
            result = FinalizeResult(pushed=True)
            payload = {
                "release_id": release_id,
                "run_id": run_id,
                "requested_mode": mode,
                "policy": policy.model_dump(mode="json"),
                "policy_source": "config",
                "gate": {"allowed": bool(allowed), "reason": blocked_reason, "decision": str(decision)},
                "outcome": "executed",
                "stop_reason": None,
                "missing_credentials": [],
                "git_commands": git_commands,
                "handoff_path": None,
                "finalization": result.__dict__,
            }
            return _write(payload), result

        if policy.policy == ReleaseFinalizationPolicyName.LOCAL_MERGE:
            git_commands.append(f"git merge --no-edit {integration_branch} (into {base_branch})")
            result = merge_integration_branch_to_base(
                repo_path=repo_path,
                integration_branch=integration_branch,
                base_branch=base_branch,
                push=False,
            )
            _report(progress, f"event=release_merged target={base_branch}")
            payload = {
                "release_id": release_id,
                "run_id": run_id,
                "requested_mode": mode,
                "policy": policy.model_dump(mode="json"),
                "policy_source": "config",
                "gate": {"allowed": bool(allowed), "reason": blocked_reason, "decision": str(decision)},
                "outcome": "executed",
                "stop_reason": None,
                "missing_credentials": [],
                "git_commands": git_commands,
                "handoff_path": None,
                "finalization": result.__dict__,
            }
            return _write(payload), result

        if policy.policy == ReleaseFinalizationPolicyName.PR_PREPARATION:
            handoff_path = release_root / "pr_handoff.json"
            handoff_payload = {
                "release_id": release_id,
                "run_id": run_id,
                "base_branch": base_branch,
                "head_branch": integration_branch,
                "head_commit": integration_commit,
                "suggested_title": f"{release_id}: finalize accepted release",
                "suggested_body": "\n".join(
                    [
                        f"Release `{release_id}` accepted with integration branch `{integration_branch}`.",
                        "",
                        "Create a PR from the head branch into the base branch.",
                        "",
                        "Fallback commands:",
                        f"- Ensure the head branch is pushed: `git push origin {integration_branch}`",
                        "- Open a PR via your hosting provider UI.",
                    ]
                ),
            }
            handoff_path.write_text(json.dumps(handoff_payload, indent=2) + "\n", encoding="utf-8")
            _report(progress, f"event=release_pr_handoff_written path={handoff_path}")
            payload = {
                "release_id": release_id,
                "run_id": run_id,
                "requested_mode": mode,
                "policy": policy.model_dump(mode="json"),
                "policy_source": "config",
                "gate": {"allowed": bool(allowed), "reason": blocked_reason, "decision": str(decision)},
                "outcome": "executed",
                "stop_reason": None,
                "missing_credentials": [],
                "git_commands": git_commands,
                "handoff_path": str(handoff_path),
                "finalization": None,
            }
            return _write(payload), None

        if policy.policy == ReleaseFinalizationPolicyName.STOP_MISSING_POLICY_OR_CREDENTIALS:
            _report(progress, "event=release_finalization_stopped reason=policy_stop")
            payload = {
                "release_id": release_id,
                "run_id": run_id,
                "requested_mode": mode,
                "policy": policy.model_dump(mode="json"),
                "policy_source": "config",
                "gate": {"allowed": bool(allowed), "reason": blocked_reason, "decision": str(decision)},
                "outcome": "stopped",
                "stop_reason": "policy_stop",
                "missing_credentials": [],
                "git_commands": git_commands,
                "handoff_path": None,
                "finalization": None,
            }
            return _write(payload), None
    except GitFinalizeError as error:
        _report(progress, f"event=release_finalization_failed error={json.dumps(str(error))}")
        result = FinalizeResult(failed_step=error.step, error=str(error))
        payload = {
            "release_id": release_id,
            "run_id": run_id,
            "requested_mode": mode,
            "policy": policy.model_dump(mode="json"),
            "policy_source": "config",
            "gate": {"allowed": bool(allowed), "reason": blocked_reason, "decision": str(decision)},
            "outcome": "failed",
            "stop_reason": None,
            "missing_credentials": [],
            "git_commands": git_commands,
            "handoff_path": str(handoff_path) if handoff_path is not None else None,
            "finalization": result.__dict__,
        }
        return _write(payload), result

    raise ValueError(f"unsupported release finalization policy: {policy.policy}")


def _ensure_no_existing_worktrees(worktree_root: Path) -> None:
    if not worktree_root.exists():
        return
    existing = sorted(
        path for path in worktree_root.iterdir() if path.is_dir() and not path.name.startswith(".")
    )
    if not existing:
        return
    listed = ", ".join(str(path) for path in existing[:5])
    suffix = "" if len(existing) <= 5 else f", ... (+{len(existing) - 5} more)"
    raise ValueError(
        "project worktree root is not clean before release start: "
        f"{listed}{suffix}. Inspect or remove stale worktrees before running run-release."
    )


def _ensure_no_existing_task_branches(
    repo_path: Path,
    release_id: str,
    tasks: list[TaskContract],
) -> None:
    existing: list[str] = []
    for task in tasks:
        branch = branch_name(release_id, task.task_id)
        result = run_process(
            ["git", "branch", "--list", branch],
            cwd=repo_path,
            timeout_seconds=30,
        )
        if result.exit_code != 0:
            raise ValueError(
                "could not inspect existing release task branches: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        if result.stdout.strip():
            existing.append(branch)

    if not existing:
        return
    listed = ", ".join(existing[:5])
    suffix = "" if len(existing) <= 5 else f", ... (+{len(existing) - 5} more)"
    raise ValueError(
        "release task branches already exist before release start: "
        f"{listed}{suffix}. Merge or delete stale branches before running run-release."
    )


def _release_decision(decisions: list[ReviewDecision]) -> Decision:
    if decisions and all(decision.decision == Decision.ACCEPTED for decision in decisions):
        return Decision.ACCEPTED
    if any(decision.decision == Decision.ESCALATED for decision in decisions):
        return Decision.ESCALATED
    return Decision.FAILED


def _write_final_review_continuation_decision(
    *,
    release_root: Path,
    release_id: str,
    feature_review_decision: FeatureReviewDecision | None,
    feature_review_path: Path | None,
    feature_review_recheck: FeatureReviewRecheckRecord | None,
    feature_review_recheck_path: Path | None,
    feature_review_proposals: list[FeatureReviewProposalRecord],
    final_integration_verification_path: Path | None,
    final_review_finding_adjudication_paths: list[Path],
    finalization_gate: dict[str, object],
) -> Path:
    unresolved_required = [
        str(item) for item in finalization_gate.get("unresolved_required_finding_ids", []) if str(item).strip()
    ]
    if (
        not unresolved_required
        and feature_review_decision is not None
        and not bool(finalization_gate.get("allowed"))
        and not (feature_review_recheck is not None and feature_review_recheck.stop_reason == "blocked_by_hard_gate")
    ):
        unresolved_required = [
            finding.finding_id
            for finding in feature_review_decision.findings
            if finding.required_repairs
        ]
    if (
        not unresolved_required
        and feature_review_path is not None
        and feature_review_path.exists()
        and not bool(finalization_gate.get("allowed"))
        and not (feature_review_recheck is not None and feature_review_recheck.stop_reason == "blocked_by_hard_gate")
    ):
        try:
            persisted_review = FeatureReviewDecision.model_validate(
                json.loads(feature_review_path.read_text(encoding="utf-8"))
            )
            unresolved_required = [
                finding.finding_id
                for finding in persisted_review.findings
                if finding.required_repairs
            ]
        except (OSError, json.JSONDecodeError, ValueError):
            unresolved_required = []
    accepted_risks = feature_review_decision.accepted_risks if feature_review_decision is not None else []
    accepted_findings = feature_review_recheck.accepted_finding_ids if feature_review_recheck is not None else []
    deferred_findings = feature_review_recheck.deferred_finding_ids if feature_review_recheck is not None else []
    proposal_paths = sorted(
        {
            str(Path(record.decision_artifact_path))
            for record in feature_review_proposals
            if record.decision_artifact_path.strip()
        }
    )
    deferred_adjudication_paths: list[Path] = []
    for adjudication_path in sorted({path for path in final_review_finding_adjudication_paths if path}):
        try:
            payload = json.loads(adjudication_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        classification = str(payload.get("classification") or "").strip()
        selected_action = str(payload.get("selected_action") or "").strip()
        if selected_action != FinalReviewFindingAdjudicationAction.DEFER.value:
            continue
        if classification not in {
            FinalReviewFindingAdjudicationClassification.BACKLOG_FOLLOW_UP.value,
            FinalReviewFindingAdjudicationClassification.SCOPE_EXPANSION.value,
            FinalReviewFindingAdjudicationClassification.DUPLICATE.value,
        }:
            continue
        deferred_adjudication_paths.append(adjudication_path)
    rerun_validator_evidence_paths: list[Path] = []
    if final_integration_verification_path is not None:
        rerun_validator_evidence_paths.append(final_integration_verification_path)
    rerun_validator_evidence_paths.extend(
        sorted((release_root / "feature_review").glob("verification_rerun_*/verification.log"))
    )
    generated_repair_contract_paths = sorted((release_root / "feature_review").glob("repairs_*/*.yaml"))
    adjudication_paths = sorted({path for path in final_review_finding_adjudication_paths if path})
    backlog_follow_up_paths = [Path(item) for item in proposal_paths] or deferred_adjudication_paths

    if unresolved_required and generated_repair_contract_paths:
        decision = FinalReviewContinuationDecision(
            release_id=release_id,
            outcome=FinalReviewContinuationOutcome.BLOCKER,
            feature_review_path=feature_review_path,
            feature_review_recheck_path=feature_review_recheck_path,
            final_integration_verification_path=final_integration_verification_path,
            finding_ids=unresolved_required,
            finding_adjudication_paths=adjudication_paths,
            generated_repair_contract_paths=generated_repair_contract_paths,
        )
    elif unresolved_required:
        decision = FinalReviewContinuationDecision(
            release_id=release_id,
            outcome=FinalReviewContinuationOutcome.HARD_STOP,
            feature_review_path=feature_review_path,
            feature_review_recheck_path=feature_review_recheck_path,
            final_integration_verification_path=final_integration_verification_path,
            finding_ids=unresolved_required,
            finding_adjudication_paths=adjudication_paths,
            hard_stop_reason="missing_generated_repair_contracts",
        )
    elif feature_review_recheck is not None and feature_review_recheck.stop_reason == "blocked_by_hard_gate":
        hard_stop_reason = "blocked_by_hard_gate"
        unresolved = list(feature_review_recheck.unresolved_finding_ids)
        if any(item.startswith(f"{release_id}:missing_release_evidence:") for item in unresolved):
            hard_stop_reason = "missing_release_evidence"
        elif f"{release_id}:schema_invalid_reviewer_output" in unresolved:
            hard_stop_reason = "schema_invalid_reviewer_output"
        decision = FinalReviewContinuationDecision(
            release_id=release_id,
            outcome=FinalReviewContinuationOutcome.HARD_STOP,
            feature_review_path=feature_review_path,
            feature_review_recheck_path=feature_review_recheck_path,
            final_integration_verification_path=final_integration_verification_path,
            finding_ids=unresolved,
            finding_adjudication_paths=adjudication_paths,
            generated_repair_contract_paths=generated_repair_contract_paths,
            hard_stop_reason=hard_stop_reason,
        )
    elif (
        feature_review_recheck is not None
        and feature_review_recheck.stop_reason == "accepted_with_rationale"
        and accepted_findings
        and accepted_risks
    ):
        decision = FinalReviewContinuationDecision(
            release_id=release_id,
            outcome=FinalReviewContinuationOutcome.ACCEPTED_RISK,
            feature_review_path=feature_review_path,
            feature_review_recheck_path=feature_review_recheck_path,
            final_integration_verification_path=final_integration_verification_path,
            finding_ids=list(accepted_findings),
            finding_adjudication_paths=adjudication_paths,
            rerun_validator_evidence_paths=rerun_validator_evidence_paths,
            accepted_risk_rationale="\n".join(accepted_risks),
        )
    elif bool(finalization_gate.get("allowed")) and backlog_follow_up_paths and not unresolved_required:
        decision = FinalReviewContinuationDecision(
            release_id=release_id,
            outcome=FinalReviewContinuationOutcome.BACKLOG_FOLLOW_UP,
            feature_review_path=feature_review_path,
            feature_review_recheck_path=feature_review_recheck_path,
            final_integration_verification_path=final_integration_verification_path,
            finding_ids=list(deferred_findings),
            finding_adjudication_paths=adjudication_paths,
            backlog_follow_up_proposal_paths=backlog_follow_up_paths,
            rerun_validator_evidence_paths=rerun_validator_evidence_paths,
        )
    else:
        decision = FinalReviewContinuationDecision(
            release_id=release_id,
            outcome=FinalReviewContinuationOutcome.HARD_STOP,
            feature_review_path=feature_review_path,
            feature_review_recheck_path=feature_review_recheck_path,
            final_integration_verification_path=final_integration_verification_path,
            finding_ids=list(unresolved_required or (feature_review_recheck.unresolved_finding_ids if feature_review_recheck else [])),
            finding_adjudication_paths=adjudication_paths,
            generated_repair_contract_paths=generated_repair_contract_paths,
            hard_stop_reason=str(finalization_gate.get("reason", "unknown")),
        )

    decision_path = release_root / "final_review_continuation_decision.json"
    decision_path.write_text(json.dumps(decision.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return decision_path


def _write_release_summary(
    *,
    runs_dir: Path,
    run_id: str,
    release_id: str,
    decision: Decision,
    task_results: list[TaskRunResult],
    log_path: Path,
    raw_log_path: Path,
    integration_branch: str,
    integration_commit: str,
    finalization: FinalizeResult | None,
    budget_path: Path,
    tuning_path: Path,
    budget_violations: list[str],
    soft_budget_findings: list[str],
    release_soft_gate_decision_path: Path | None,
    feature_review_path: Path | None,
    feature_review_recheck_path: Path | None,
    feature_review_proposals: list[FeatureReviewProposalRecord],
    feature_review_prompt_path: Path | None,
    feature_review_stdout_path: Path | None,
    feature_review_stderr_path: Path | None,
    feature_review_metadata_path: Path | None,
    feature_review_bundle_manifest_paths: list[Path],
    feature_review_output_normalization_decision_path: Path | None,
    feature_review_normalized_artifact_path: Path | None,
    final_review_continuation_decision_path: Path,
    finalization_gate: dict[str, object],
    finalization_decision_path: Path | None,
    final_integration_verification_path: Path | None,
    final_integration_verification: dict[str, object] | None,
    scope_risk_budget_policy_decision_paths: list[Path],
    scope_risk_budget_policy_gate: dict[str, object] | None,
) -> Path:
    summary_dir = runs_dir / run_id
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summary_dir / "release_summary.json"
    final_review_continuation_payload = _read_json_object(final_review_continuation_decision_path)
    final_review_outcome = final_review_continuation_payload.get("outcome")
    final_review_finding_ids = final_review_continuation_payload.get("finding_ids")
    final_review_adjudication_paths = final_review_continuation_payload.get("finding_adjudication_paths")
    final_review_backlog_follow_up_paths = final_review_continuation_payload.get("backlog_follow_up_proposal_paths")
    final_review_hard_stop_reason = final_review_continuation_payload.get("hard_stop_reason")
    summary = {
        "run_id": run_id,
        "release_id": release_id,
        "decision": decision,
        "log_path": str(log_path),
        "raw_log_path": str(raw_log_path),
        "metrics_path": str(summary_dir / "release_metrics.json"),
        "budget_path": str(budget_path),
        "tuning_path": str(tuning_path),
        "budget_violations": budget_violations,
        "soft_budget_findings": soft_budget_findings,
        "release_soft_gate_decision_path": str(release_soft_gate_decision_path) if release_soft_gate_decision_path else None,
        "feature_review_path": str(feature_review_path) if feature_review_path else None,
        "feature_review_recheck_path": str(feature_review_recheck_path) if feature_review_recheck_path else None,
        "feature_review_proposals": [record.model_dump(mode="json") for record in feature_review_proposals],
        "feature_review_prompt_path": str(feature_review_prompt_path) if feature_review_prompt_path else None,
        "feature_review_stdout_path": str(feature_review_stdout_path) if feature_review_stdout_path else None,
        "feature_review_stderr_path": str(feature_review_stderr_path) if feature_review_stderr_path else None,
        "feature_review_metadata_path": str(feature_review_metadata_path) if feature_review_metadata_path else None,
        "feature_review_bundle_manifest_paths": [str(path) for path in feature_review_bundle_manifest_paths],
        "feature_review_latest_bundle_manifest_path": (
            str(feature_review_bundle_manifest_paths[-1]) if feature_review_bundle_manifest_paths else None
        ),
        "feature_review_output_normalization_decision_path": (
            str(feature_review_output_normalization_decision_path)
            if feature_review_output_normalization_decision_path
            else None
        ),
        "feature_review_normalized_artifact_path": (
            str(feature_review_normalized_artifact_path) if feature_review_normalized_artifact_path else None
        ),
        "final_review_continuation_decision_path": str(final_review_continuation_decision_path),
        "final_review_continuation_outcome": final_review_outcome if isinstance(final_review_outcome, str) else None,
        "final_review_continuation_finding_ids": (
            [str(item) for item in final_review_finding_ids if str(item).strip()]
            if isinstance(final_review_finding_ids, list)
            else []
        ),
        "final_review_finding_adjudication_paths": (
            [str(item) for item in final_review_adjudication_paths if str(item).strip()]
            if isinstance(final_review_adjudication_paths, list)
            else []
        ),
        "final_review_backlog_follow_up_paths": (
            [str(item) for item in final_review_backlog_follow_up_paths if str(item).strip()]
            if isinstance(final_review_backlog_follow_up_paths, list)
            else []
        ),
        "final_review_hard_stop_reason": (
            str(final_review_hard_stop_reason).strip()
            if isinstance(final_review_hard_stop_reason, str) and final_review_hard_stop_reason.strip()
            else None
        ),
        "finalization_gate": finalization_gate,
        "finalization_decision_path": str(finalization_decision_path) if finalization_decision_path else None,
        "integration_branch": integration_branch,
        "integration_commit": integration_commit,
        "final_integration_verification_path": (
            str(final_integration_verification_path) if final_integration_verification_path else None
        ),
        "final_integration_verification": final_integration_verification,
        "scope_risk_budget_policy_decision_paths": [
            str(path) for path in scope_risk_budget_policy_decision_paths
        ],
        "scope_risk_budget_policy_gate": scope_risk_budget_policy_gate,
        "finalization": {
            "merged": finalization.merged,
            "pushed": finalization.pushed,
            "failed_step": finalization.failed_step,
            "error": finalization.error,
        }
        if finalization
        else None,
        "tasks": [
            {
                "task_id": result.decision.task_id,
                "run_id": result.run_id,
                "decision": result.decision.decision,
                "rationale": result.decision.rationale,
                "bundle_path": str(result.bundle_path),
                "commit_hash": result.finalize.commit_hash if result.finalize else None,
                "merged": result.finalize.merged if result.finalize else False,
                "pushed": result.finalize.pushed if result.finalize else False,
            }
            for result in task_results
        ],
    }
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary_path


def _git_rev_parse(repo_path: Path, ref: str) -> str:
    result = run_process(
        ["git", "rev-parse", "--verify", ref],
        cwd=repo_path,
        timeout_seconds=120,
    )
    if result.exit_code != 0:
        raise GitFinalizeError(result.stderr.strip() or result.stdout.strip() or f"git ref not found: {ref}")
    return result.stdout.strip()


def _write_release_review(
    *,
    runs_dir: Path,
    run_id: str,
    release_id: str,
    decision: Decision,
    task_results: list[TaskRunResult],
    integration_branch: str,
    finalization: FinalizeResult | None,
    metrics_path: Path,
    budget_path: Path,
    tuning_path: Path,
    budget_violations: list[str],
    soft_budget_findings: list[str],
    release_soft_gate_decision_path: Path | None,
    feature_review_decision: FeatureReviewDecision | None,
    feature_review_path: Path | None,
    feature_review_recheck: FeatureReviewRecheckRecord | None,
    feature_review_recheck_path: Path | None,
    feature_review_prompt_path: Path | None,
    feature_review_stdout_path: Path | None,
    feature_review_stderr_path: Path | None,
    feature_review_metadata_path: Path | None,
    feature_review_output_normalization_decision_path: Path | None,
    feature_review_normalized_artifact_path: Path | None,
    final_review_continuation_decision_path: Path,
    finalization_gate: dict[str, object],
    final_integration_verification_path: Path | None,
    scope_risk_budget_policy_decision_paths: list[Path],
    scope_risk_budget_policy_gate: dict[str, object] | None,
) -> Path:
    review_path = runs_dir / run_id / "release_review.md"
    lines = [
        f"# Release Review: {release_id}",
        "",
        f"- Decision: `{decision}`",
        f"- Integration branch: `{integration_branch}`",
        f"- Tasks completed: `{len(task_results)}`",
        f"- Metrics: `{metrics_path}`",
        f"- Budget: `{budget_path}`",
        f"- Tuning: `{tuning_path}`",
        f"- Final integration verification: `{final_integration_verification_path or 'not_run'}`",
        "",
        "## Budget",
        "",
    ]
    if budget_violations:
        lines.extend(f"- Violation: {violation}" for violation in budget_violations)
    else:
        lines.append("- No configured release-level budget limits were exceeded.")
    if soft_budget_findings:
        lines.append("- Soft findings accepted with mitigation:")
        lines.extend(f"- Soft finding: {finding}" for finding in soft_budget_findings)
    if release_soft_gate_decision_path is not None:
        lines.append(f"- Soft decision artifact: `{release_soft_gate_decision_path}`")
    if scope_risk_budget_policy_gate is not None:
        lines.extend(
            [
                "",
                "## Scope Risk",
                "",
                f"- Allowed: `{scope_risk_budget_policy_gate.get('allowed')}`",
                f"- Gate reason: `{scope_risk_budget_policy_gate.get('reason')}`",
                f"- Required task ids: `{len(scope_risk_budget_policy_gate.get('required_task_ids', []))}`",
                f"- Decision artifacts: `{len(scope_risk_budget_policy_decision_paths)}`",
            ]
        )
        if scope_risk_budget_policy_gate.get("blocking_reasons"):
            lines.append("- Blocking reasons:")
            for reason in scope_risk_budget_policy_gate.get("blocking_reasons", []):
                cleaned = str(reason).strip()
                if cleaned:
                    lines.append(f"- {cleaned}")
        if scope_risk_budget_policy_decision_paths:
            lines.append("- Scope-risk decision artifacts:")
            lines.extend(f"- `{path}`" for path in scope_risk_budget_policy_decision_paths)
    if feature_review_path is not None:
        lines.extend(
            [
                "",
                "## Feature Review",
                "",
                f"- Artifact: `{feature_review_path}`",
            ]
        )
        if feature_review_prompt_path is not None:
            lines.append(f"- Prompt: `{feature_review_prompt_path}`")
        if feature_review_stdout_path is not None:
            lines.append(f"- Reviewer stdout: `{feature_review_stdout_path}`")
        if feature_review_stderr_path is not None:
            lines.append(f"- Reviewer stderr: `{feature_review_stderr_path}`")
        if feature_review_metadata_path is not None:
            lines.append(f"- Reviewer metadata: `{feature_review_metadata_path}`")
        if feature_review_output_normalization_decision_path is not None:
            lines.append(
                f"- Output normalization decision: `{feature_review_output_normalization_decision_path}`"
            )
        if feature_review_normalized_artifact_path is not None:
            lines.append(f"- Normalized reviewer output: `{feature_review_normalized_artifact_path}`")
        if feature_review_decision is not None:
            lines.append(f"- Recommendation: `{feature_review_decision.recommendation.value}`")
            lines.append(f"- Findings: `{len(feature_review_decision.findings)}`")
        if feature_review_recheck_path is not None:
            lines.append(f"- Recheck artifact: `{feature_review_recheck_path}`")
        if feature_review_recheck is not None and feature_review_recheck.stop_reason is not None:
            lines.append(f"- Recheck status: `{feature_review_recheck.stop_reason}`")
        lines.append(f"- Continuation decision artifact: `{final_review_continuation_decision_path}`")
        continuation_payload = _read_json_object(final_review_continuation_decision_path)
        continuation_outcome = continuation_payload.get("outcome")
        if isinstance(continuation_outcome, str) and continuation_outcome.strip():
            lines.append(f"- Final review continuation outcome: `{continuation_outcome}`")
        continuation_hard_stop = continuation_payload.get("hard_stop_reason")
        if isinstance(continuation_hard_stop, str) and continuation_hard_stop.strip():
            lines.append(f"- Final review hard-stop reason: `{continuation_hard_stop}`")
        adjudication_paths = continuation_payload.get("finding_adjudication_paths")
        if isinstance(adjudication_paths, list):
            cleaned_adjudication_paths = [str(item) for item in adjudication_paths if str(item).strip()]
            lines.append(f"- Final review adjudication artifacts: `{len(cleaned_adjudication_paths)}`")
            lines.extend(f"- `{path}`" for path in cleaned_adjudication_paths)
        lines.append(
            "- The continuation decision artifact links the final verification evidence, repair contracts, "
            "and backlog follow-up proposal paths."
        )
    lines.extend([
        "",
        "## Task Results",
        "",
    ])
    for result in task_results:
        finalize = result.finalize
        lines.extend(
            [
                f"### {result.decision.task_id}",
                "",
                f"- Decision: `{result.decision.decision}`",
                f"- Rationale: {result.decision.rationale}",
                f"- Evidence: `{result.bundle_path}`",
                f"- Commit: `{finalize.commit_hash if finalize and finalize.commit_hash else 'none'}`",
                f"- Merged: `{bool(finalize and finalize.merged)}`",
                f"- Pushed: `{bool(finalize and finalize.pushed)}`",
                "",
            ]
        )
        if result.decision.risks:
            lines.append("Risks:")
            lines.extend(f"- {risk}" for risk in result.decision.risks)
            lines.append("")
        if result.decision.follow_up_tasks:
            lines.append("Follow-up tasks:")
            lines.extend(f"- {task}" for task in result.decision.follow_up_tasks)
            lines.append("")
    if any(result.decision.decision != Decision.ACCEPTED for result in task_results):
        lines.extend(
            [
                "## Release Follow-Up",
                "",
                "- Inspect non-accepted task evidence before rerunning or expanding the release.",
                "",
            ]
        )
    if finalization is not None:
        lines.extend(
            [
                "## Release Finalization",
                "",
                f"- Allowed: `{finalization_gate['allowed']}`",
                f"- Gate reason: `{finalization_gate['reason']}`",
                f"- Unresolved required findings: `{len(finalization_gate['unresolved_required_finding_ids'])}`",
                f"- Merged: `{finalization.merged}`",
                f"- Pushed: `{finalization.pushed}`",
                f"- Error: `{finalization.error or 'none'}`",
                "",
            ]
        )
    elif not bool(finalization_gate["allowed"]):
        lines.extend(
            [
                "## Release Finalization",
                "",
                f"- Allowed: `{finalization_gate['allowed']}`",
                f"- Gate reason: `{finalization_gate['reason']}`",
                f"- Unresolved required findings: `{len(finalization_gate['unresolved_required_finding_ids'])}`",
                "",
            ]
        )
    review_path.write_text("\n".join(lines), encoding="utf-8")
    return review_path


def _build_release_finalization_gate(
    *,
    decision: Decision,
    feature_review_decision: FeatureReviewDecision | None,
    feature_review_recheck: FeatureReviewRecheckRecord | None,
) -> dict[str, object]:
    unresolved_finding_ids = set(feature_review_recheck.unresolved_finding_ids) if feature_review_recheck else set()
    required_finding_ids_from_decision = (
        {
            finding.finding_id
            for finding in feature_review_decision.findings
            if finding.required_repairs
        }
        if feature_review_decision
        else set()
    )
    optional_finding_ids_from_decision = (
        {
            finding.finding_id
            for finding in feature_review_decision.findings
            if not finding.required_repairs and finding.optional_follow_ups
        }
        if feature_review_decision
        else set()
    )
    unresolved_required_finding_ids = sorted(unresolved_finding_ids.intersection(required_finding_ids_from_decision))
    if (
        not unresolved_required_finding_ids
        and unresolved_finding_ids
        and not required_finding_ids_from_decision
        and feature_review_recheck is not None
        and feature_review_recheck.stop_reason in {"blocked_by_retry_budget", "blocked_by_hard_gate"}
    ):
        # Recheck unresolved findings are authoritative when the latest decision no longer
        # carries explicit required findings but the loop still ended in a blocked state.
        unresolved_required_finding_ids = sorted(unresolved_finding_ids.difference(optional_finding_ids_from_decision))
    if unresolved_required_finding_ids:
        gate = ReleaseFinalizationGate(
            allowed=False,
            reason="unresolved_required_findings",
            unresolved_required_finding_ids=unresolved_required_finding_ids,
            decision=decision,
        )
        return gate.model_dump(mode="json")
    if decision != Decision.ACCEPTED:
        gate = ReleaseFinalizationGate(
            allowed=False,
            reason="release_decision_not_accepted",
            unresolved_required_finding_ids=[],
            decision=decision,
        )
        return gate.model_dump(mode="json")
    gate = ReleaseFinalizationGate(
        allowed=True,
        reason="allowed",
        unresolved_required_finding_ids=[],
        decision=decision,
    )
    return gate.model_dump(mode="json")


def _build_release_metrics(
    *,
    run_id: str,
    release_id: str,
    decision: Decision,
    task_results: list[TaskRunResult],
    raw_log_path: Path,
    runs_dir: Path,
) -> dict[str, object]:
    task_metrics = [_task_metrics(result, raw_log_path) for result in task_results]
    compact_governance = _build_compact_governance_metrics(
        run_id=run_id,
        runs_dir=runs_dir,
        task_metrics=task_metrics,
    )
    model_attempts: dict[str, dict[str, object]] = {}
    for task in task_metrics:
        for attempt in task["executor_attempts"]:
            model = str(attempt.get("model") or "<none>")
            entry = model_attempts.setdefault(
                model,
                {
                    "attempts": 0,
                    "successful_attempts": 0,
                    "failed_attempts": 0,
                    "duration_seconds": 0.0,
                    "prompt_chars": 0,
                    "stdout_chars": 0,
                    "stderr_chars": 0,
                },
            )
            entry["attempts"] = int(entry["attempts"]) + 1
            if int(attempt.get("exit_code", 1)) == 0:
                entry["successful_attempts"] = int(entry["successful_attempts"]) + 1
            else:
                entry["failed_attempts"] = int(entry["failed_attempts"]) + 1
            entry["duration_seconds"] = float(entry["duration_seconds"]) + float(attempt.get("duration_seconds", 0.0))
            entry["prompt_chars"] = int(entry["prompt_chars"]) + int(attempt.get("prompt_chars", 0))
            entry["stdout_chars"] = int(entry["stdout_chars"]) + int(attempt.get("stdout_chars", 0))
            entry["stderr_chars"] = int(entry["stderr_chars"]) + int(attempt.get("stderr_chars", 0))

    totals = {
        "tasks": len(task_metrics),
        "accepted_tasks": sum(1 for task in task_metrics if task["decision"] == Decision.ACCEPTED),
        "executor_attempts": sum(len(task["executor_attempts"]) for task in task_metrics),
        "prompt_chars": sum(int(task["prompt_chars"]) for task in task_metrics),
        "context_chars": sum(int(task["context_chars"]) for task in task_metrics),
        "stdout_chars": sum(int(task["stdout_chars"]) for task in task_metrics),
        "stderr_chars": sum(int(task["stderr_chars"]) for task in task_metrics),
        "diff_lines": sum(int(task["diff_lines"]) for task in task_metrics),
        "changed_files": sum(int(task["changed_file_count"]) for task in task_metrics),
        "verification_duration_seconds": round(
            sum(float(task["verification_duration_seconds"]) for task in task_metrics),
            3,
        ),
        "executor_duration_seconds": round(
            sum(float(attempt.get("duration_seconds", 0.0)) for task in task_metrics for attempt in task["executor_attempts"]),
            3,
        ),
    }
    metrics = {
        "run_id": run_id,
        "release_id": release_id,
        "decision": decision,
        "totals": totals,
        "compact_governance": compact_governance,
        "strong_model_calls": _strong_model_calls(runs_dir, release_id),
        "model_attempts": model_attempts,
        "tasks": task_metrics,
        "notes": [
            "Character counts are local proxies for cost analysis; token counts require provider usage metadata.",
            "prompt_chars includes the full executor prompt, while context_chars tracks only context bundle content reported by orchestration.",
        ],
    }
    return metrics


def _build_compact_governance_metrics(
    *,
    run_id: str,
    runs_dir: Path,
    task_metrics: list[dict[str, object]],
) -> dict[str, object]:
    release_root = runs_dir / run_id
    runtime_supervisor_dir = release_root / "runtime_supervisor"
    feature_review_dir = release_root / "feature_review"
    model_fallback_count = 0
    for task in task_metrics:
        attempts = task.get("executor_attempts")
        if not isinstance(attempts, list) or not attempts:
            continue
        first_model = str((attempts[0] or {}).get("model") or "<none>")
        fallback_attempts = 0
        for attempt in attempts[1:]:
            if not isinstance(attempt, dict):
                continue
            model = str(attempt.get("model") or "<none>")
            if model != first_model:
                fallback_attempts += 1
        model_fallback_count += fallback_attempts

    repair_wave_count = len(
        [path for path in feature_review_dir.glob("repairs_*") if path.is_dir()]
    )
    review_wave_count = 0
    if (release_root / "feature_review.json").exists():
        review_wave_count = 1 + repair_wave_count

    runtime_repair_attempt_count = 0
    runtime_repair_success_count = 0
    runtime_repair_stop_count = 0
    for repair_path in runtime_supervisor_dir.glob("repair_*.json"):
        payload = _read_json_object(repair_path)
        attempts = payload.get("attempts")
        if not isinstance(attempts, list):
            continue
        runtime_repair_attempt_count += len(attempts)
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            if str(attempt.get("decision") or "").strip().lower() == "stop":
                runtime_repair_stop_count += 1
        final_result = payload.get("final_result")
        if isinstance(final_result, dict) and str(final_result.get("decision") or "").strip().lower() == "accepted":
            runtime_repair_success_count += 1

    admission_repair_count = 0
    admission_repairs = _read_json_object(runtime_supervisor_dir / "planner_admission_repairs.json")
    records = admission_repairs.get("records")
    if isinstance(records, list):
        admission_repair_count = len(records)

    scope_risk_overage_count = 0
    scope_risk_blocked_count = 0
    for decision_path in (release_root / "supervisor_decisions").glob("scope_risk_budget_policy__*.json"):
        decision = _read_json_object(decision_path)
        configured_changed_files_limit = decision.get("configured_changed_files_limit")
        actual_changed_files = decision.get("actual_changed_files")
        configured_diff_size_limit = decision.get("configured_diff_size_limit")
        actual_diff_size = decision.get("actual_diff_size")
        changed_files_over = (
            isinstance(configured_changed_files_limit, int)
            and isinstance(actual_changed_files, int)
            and actual_changed_files > configured_changed_files_limit
        )
        diff_size_over = (
            isinstance(configured_diff_size_limit, int)
            and isinstance(actual_diff_size, int)
            and actual_diff_size > configured_diff_size_limit
        )
        if changed_files_over or diff_size_over:
            scope_risk_overage_count += 1
        if str(decision.get("outcome") or "").strip() == ScopeRiskOutcome.STOPPED.value:
            scope_risk_blocked_count += 1

    continuation_payload = _read_json_object(release_root / "final_review_continuation_decision.json")
    finding_adjudication_paths = continuation_payload.get("finding_adjudication_paths")
    final_review_adjudication_count = len(finding_adjudication_paths) if isinstance(finding_adjudication_paths, list) else 0
    final_review_continuation_outcome = (
        str(continuation_payload.get("outcome")).strip()
        if str(continuation_payload.get("outcome") or "").strip()
        else None
    )
    final_review_hard_stop_reason = (
        str(continuation_payload.get("hard_stop_reason")).strip()
        if str(continuation_payload.get("hard_stop_reason") or "").strip()
        else None
    )

    finalization_payload = _read_json_object(release_root / "finalization_decision.json")
    finalization_outcome = (
        str(finalization_payload.get("outcome")).strip()
        if str(finalization_payload.get("outcome") or "").strip()
        else None
    )
    finalization_stop_reason = (
        str(finalization_payload.get("stop_reason")).strip()
        if str(finalization_payload.get("stop_reason") or "").strip()
        else None
    )
    finalization_gate_reason = (
        str(finalization_payload.get("blocked_reason")).strip()
        if str(finalization_payload.get("blocked_reason") or "").strip()
        else None
    )

    return {
        "model_fallback_count": model_fallback_count,
        "review_wave_count": review_wave_count,
        "feature_review_repair_wave_count": repair_wave_count,
        "runtime_repair_attempt_count": runtime_repair_attempt_count,
        "runtime_repair_success_count": runtime_repair_success_count,
        "runtime_repair_stop_count": runtime_repair_stop_count,
        "admission_repair_count": admission_repair_count,
        "scope_risk_overage_count": scope_risk_overage_count,
        "scope_risk_blocked_count": scope_risk_blocked_count,
        "final_review_adjudication_count": final_review_adjudication_count,
        "final_review_continuation_outcome": final_review_continuation_outcome,
        "final_review_hard_stop_reason": final_review_hard_stop_reason,
        "finalization_outcome": finalization_outcome,
        "finalization_stop_reason": finalization_stop_reason,
        "finalization_gate_reason": finalization_gate_reason,
    }


def _write_release_metrics(
    *,
    runs_dir: Path,
    run_id: str,
    metrics: dict[str, object],
) -> Path:
    metrics_path = runs_dir / run_id / "release_metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    return metrics_path


def _write_release_budget(*, runs_dir: Path, run_id: str, ledger) -> Path:
    budget_path = runs_dir / run_id / "release_budget.json"
    budget_path.write_text(json.dumps(ledger.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return budget_path


def _write_release_tuning(*, runs_dir: Path, run_id: str, tuning_report) -> Path:
    tuning_path = runs_dir / run_id / "release_tuning.md"
    tuning_path.write_text(tuning_report.render_markdown(), encoding="utf-8")
    return tuning_path


def _evaluate_release_budget(ledger) -> dict[str, list[str]]:
    severe_violations: list[str] = []
    soft_findings: list[str] = []
    for entry in ledger.usage:
        if entry.scope != "release" or entry.over_by is None or entry.over_by <= 0:
            continue
        message = (
            f"{entry.name} exceeded budget: actual {entry.actual} {entry.unit} "
            f"over configured {entry.configured}"
        )
        if _is_soft_release_budget_overage(entry.configured, entry.over_by):
            soft_findings.append(message)
        else:
            severe_violations.append(message)
    return {"severe_violations": severe_violations, "soft_findings": soft_findings}


def _release_decision_with_budget(decision: Decision, severe_budget_violations: list[str]) -> Decision:
    if severe_budget_violations and decision == Decision.ACCEPTED:
        return Decision.FAILED
    return decision


def _is_soft_release_budget_overage(configured: int | float | None, over_by: int | float) -> bool:
    if configured is None or configured <= 0:
        return False
    return (float(over_by) / float(configured)) <= _RELEASE_BUDGET_SOFT_OVERAGE_RATIO


def _write_release_budget_soft_decision(
    *,
    runs_dir: Path,
    run_id: str,
    release_id: str,
    findings: list[str],
    progress: Callable[[str], None] | None,
) -> Path:
    finding_records: list[TaskSoftGateDecisionRecord] = []
    for index, finding in enumerate(findings, start=1):
        finding_id = f"release-budget-{index}"
        finding_records.append(
            TaskSoftGateDecisionRecord(
                task_id="release-budget",
                finding=SoftGateFinding(
                    finding_id=finding_id,
                    severity=SoftGateSeverity.MODERATE,
                    risk=finding,
                    recommended_actions=["Inspect release_budget.json and release_tuning.md before finalizing downstream integrations."],
                    evidence_paths=[runs_dir / run_id / "release_budget.json", runs_dir / run_id / "release_tuning.md"],
                ),
                decision=SoftGateDecision(
                    finding_id=finding_id,
                    decision=SoftGateDecisionOutcome.ACCEPT_WITH_MITIGATION,
                    rationale="Release-level overage remained within the configured soft threshold.",
                    fallback_plan="Split planned follow-up scope or tighten model usage on the next run if this repeats.",
                    validators_rerun=["release_budget.json", "release_metrics.json", "release_tuning.md"],
                    evidence_paths=[runs_dir / run_id / "release_metrics.json"],
                ),
            )
        )
    artifact_path = write_release_soft_gate_decisions(
        runs_dir / run_id,
        ReleaseSoftGateDecisionRecord(release_id=release_id, decisions=finding_records),
    )
    _report(progress, f"event=release_soft_gate_decisions path={artifact_path}")
    return artifact_path


def _write_release_log_summary(
    *,
    log_path: Path,
    raw_log_path: Path,
    release_id: str,
    decision: Decision,
    task_results: list[TaskRunResult],
    metrics_path: Path,
    budget_path: Path,
    tuning_path: Path,
    review_path: Path,
) -> None:
    task_lines = []
    for result in task_results:
        finalize = result.finalize
        commit = finalize.commit_hash[:12] if finalize and finalize.commit_hash else "none"
        merged = bool(finalize and finalize.merged)
        task_lines.append(
            f"- {result.decision.task_id}: {result.decision.decision} "
            f"(commit={commit}, merged={merged})"
        )
    summary_lines = [
        "",
        _style("🧾 Release Summary", "bold"),
        f"🚀 Release: {_style(release_id, 'cyan')}",
        f"{_status_icon(str(decision))} Decision: {_style(str(decision), _decision_style(str(decision)))}",
        f"📦 Tasks completed: {len(task_results)}",
        *task_lines,
        f"🧑‍⚖️ Review: {_style(str(review_path), 'dim')}",
        f"📊 Metrics: {_style(str(metrics_path), 'dim')}",
        f"💰 Budget: {_style(str(budget_path), 'dim')}",
        f"🛠️ Tuning: {_style(str(tuning_path), 'dim')}",
        _style("Good luck, future humans. 🧑‍🚀🛠️🍀", "green"),
    ]
    timestamp = datetime.now(UTC).isoformat()
    payload = "".join(f"{timestamp} {line}\n" if line else "\n" for line in summary_lines)
    with log_path.open("a", encoding="utf-8") as file:
        file.write(payload)
    with raw_log_path.open("a", encoding="utf-8") as file:
        file.write(payload)


_COMPACT_FINAL_FOLLOW_UP_CLASSIFICATIONS = {
    "accepted_risk",
    "soft_observability",
    "backlog_follow_up",
    "duplicate",
    "false_positive",
    "verification_only",
    "scope_expansion",
}


def _persist_compact_final_review_follow_up_memory(
    *,
    config_repo_path: Path,
    repo_state_path: Path | None,
    release_id: str,
    continuation_decision_path: Path,
) -> Path | None:
    if repo_state_path is None or not continuation_decision_path.exists():
        return None
    root = repo_state_path if repo_state_path.is_absolute() else config_repo_path / repo_state_path
    backlog_state_path = root / "backlog_state.yaml"
    store = StateStore(backlog_state_path)
    try:
        continuation = FinalReviewContinuationDecision.model_validate(
            json.loads(continuation_decision_path.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if continuation.outcome in {FinalReviewContinuationOutcome.BLOCKER, FinalReviewContinuationOutcome.HARD_STOP}:
        return None
    continuation_dir = continuation_decision_path.parent
    wrote_memory = False
    for adjudication_path in continuation.finding_adjudication_paths:
        resolved_adjudication_path = (
            adjudication_path
            if adjudication_path.is_absolute()
            else (continuation_dir / adjudication_path)
        )
        try:
            payload = json.loads(resolved_adjudication_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        classification = str(payload.get("classification") or "").strip()
        if classification not in _COMPACT_FINAL_FOLLOW_UP_CLASSIFICATIONS:
            continue
        finding_id = str(payload.get("finding_id") or "").strip()
        rationale = str(payload.get("rationale") or "").strip()
        if not finding_id or not rationale:
            continue
        evidence_paths = [
            Path(value)
            for value in payload.get("evidence_paths", [])
            if isinstance(value, str) and value.strip()
        ]
        if not evidence_paths:
            derived_paths: list[Path] = []

            def resolve_from_continuation(path: Path | None) -> None:
                if path is None:
                    return
                candidate = path if path.is_absolute() else (continuation_dir / path)
                derived_paths.append(candidate)

            resolve_from_continuation(continuation.feature_review_path)
            resolve_from_continuation(continuation.feature_review_recheck_path)
            resolve_from_continuation(continuation.final_integration_verification_path)
            for path in continuation.rerun_validator_evidence_paths:
                resolve_from_continuation(path)
            derived_paths.append(resolved_adjudication_path)
            derived_paths.append(continuation_decision_path)

            seen: set[Path] = set()
            evidence_paths = []
            for path in derived_paths:
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                evidence_paths.append(resolved)

        if not evidence_paths:
            continue
        validators_rerun = [
            value.strip()
            for value in payload.get("validators_to_rerun", [])
            if isinstance(value, str) and value.strip()
        ]
        fallback_plan = str(payload.get("fallback_plan") or "").strip() or None
        store.add_release_final_review_follow_up_memory(
            release_id,
            FinalReviewFollowUpMemoryReference(
                release_id=release_id,
                finding_id=finding_id,
                classification=classification,
                rationale_summary=rationale,
                evidence_paths=evidence_paths,
                fallback_plan=fallback_plan,
                validators_rerun=validators_rerun,
                adjudication_artifact_path=resolved_adjudication_path,
                continuation_decision_path=continuation_decision_path,
                recorded_at=datetime.now(UTC),
            ),
        )
        wrote_memory = True
    return backlog_state_path if wrote_memory else None


def _task_metrics(result: TaskRunResult, raw_log_path: Path) -> dict[str, object]:
    bundle_path = result.bundle_path
    attempts = _read_json_list(bundle_path / "executor_attempts.json")
    run_state = _read_json_object(bundle_path / "run_state.json")
    prompt_chars = _text_len(bundle_path / "executor_prompt.md")
    changed_files = _read_lines(bundle_path / "changed_files.txt")
    verification_results = run_state.get("verification_results", [])
    return {
        "task_id": result.decision.task_id,
        "run_id": result.run_id,
        "decision": result.decision.decision,
        "rationale": result.decision.rationale,
        "bundle_path": str(bundle_path),
        "commit_hash": result.finalize.commit_hash if result.finalize else None,
        "merged": result.finalize.merged if result.finalize else False,
        "pushed": result.finalize.pushed if result.finalize else False,
        "context_chars": _context_chars_for_task(raw_log_path, result.decision.task_id),
        "prompt_chars": prompt_chars,
        "stdout_chars": sum(int(attempt.get("stdout_chars", 0)) for attempt in attempts),
        "stderr_chars": sum(int(attempt.get("stderr_chars", 0)) for attempt in attempts),
        "diff_lines": int(run_state.get("diff_lines", 0)),
        "changed_file_count": len(changed_files),
        "changed_files": changed_files,
        "verification_command_count": len(verification_results),
        "verification_duration_seconds": round(
            sum(float(item.get("duration_seconds", 0.0)) for item in verification_results),
            3,
        ),
        "executor_attempts": attempts,
        "failure_diagnosis_path": str(bundle_path / "failure_diagnosis.yaml")
        if (bundle_path / "failure_diagnosis.yaml").exists()
        else None,
    }


def _read_json_object(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _read_json_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def _strong_model_calls(runs_dir: Path, release_id: str) -> int:
    ledger_path = runs_dir / release_id / "budget_ledger.json"
    return sum(1 for entry in _read_json_list(ledger_path) if entry.get("kind") == "strong_model")


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _text_len(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8"))


def _context_chars_for_task(raw_log_path: Path, task_id: str) -> int:
    if not raw_log_path.exists():
        return 0
    marker = f"event=context_loaded task={task_id} "
    for line in raw_log_path.read_text(encoding="utf-8").splitlines():
        if marker not in line:
            continue
        for part in line.split():
            if part.startswith("chars="):
                try:
                    return int(part.removeprefix("chars="))
                except ValueError:
                    return 0
    return 0


def analyze_contract_overlaps(
    tasks: list[TaskContract],
    *,
    unsafe_overlap_paths: list[str] | None = None,
) -> ReleaseOverlapReport:
    findings: list[OverlapFinding] = []
    unsafe_overlap_paths = unsafe_overlap_paths or []
    for index, first in enumerate(tasks):
        for second in tasks[index + 1 :]:
            for first_pattern in first.allowed_files:
                for second_pattern in second.allowed_files:
                    severity = _overlap_severity(
                        first_pattern,
                        second_pattern,
                        unsafe_overlap_paths=unsafe_overlap_paths,
                    )
                    if severity is not None:
                        findings.append(
                            OverlapFinding(
                                first_task_id=first.task_id,
                                second_task_id=second.task_id,
                                pattern=f"{first_pattern} <-> {second_pattern}",
                                severity=severity,
                        )
                        )
    return ReleaseOverlapReport(findings=findings)


def _write_release_overlap_report_artifact(
    *,
    release_root: Path,
    overlap_report: ReleaseOverlapReport,
) -> Path:
    artifact_path = release_root / "release_overlap_report.json"
    artifact_path.write_text(
        json.dumps(overlap_report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return artifact_path


def _release_overlap_report_sha256(overlap_report: ReleaseOverlapReport) -> str:
    payload = json.dumps(overlap_report.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _release_scheduling_staleness_inputs(
    *,
    config_repo_path: Path,
    base_branch: str,
    release_id: str,
    execution_mode: str,
    selected_contracts: list[Path],
    selected_tasks: list[TaskContract],
    overlap_report: ReleaseOverlapReport,
) -> ReleaseSchedulingStalenessInputs:
    selected_contract_paths = [path.resolve() for path in selected_contracts]
    selected_task_ids = [task.task_id for task in selected_tasks]
    overlap_report_sha256 = _release_overlap_report_sha256(overlap_report)
    base_branch_head_commit = git_text(config_repo_path, ["rev-parse", base_branch]).strip()
    release_inputs_sha256 = sha256(
        json.dumps(
            {
                "release_id": release_id,
                "execution_mode": execution_mode,
                "selected_task_ids": selected_task_ids,
                "selected_contract_paths": [str(path) for path in selected_contract_paths],
                "overlap_report_sha256": overlap_report_sha256,
                "base_branch_head_commit": base_branch_head_commit,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return ReleaseSchedulingStalenessInputs(
        execution_mode=execution_mode,  # type: ignore[arg-type]
        selected_task_ids=selected_task_ids,
        selected_contract_paths=selected_contract_paths,
        overlap_report_sha256=overlap_report_sha256,
        base_branch_head_commit=base_branch_head_commit,
        release_inputs_sha256=release_inputs_sha256,
    )


def _release_scheduling_action_for_execution_mode(
    *,
    execution_mode: str,
    overlap_report: ReleaseOverlapReport,
) -> ReleaseSchedulingAction:
    if overlap_report.findings:
        return ReleaseSchedulingAction.SEQUENTIAL
    if execution_mode == "sequential":
        return ReleaseSchedulingAction.SEQUENTIAL
    return ReleaseSchedulingAction.PARALLEL


def _release_scheduling_outcome_for_action(action: ReleaseSchedulingAction) -> SchedulingOutcome:
    if action == ReleaseSchedulingAction.PARALLEL:
        return SchedulingOutcome.PROCEED_PARALLEL
    if action == ReleaseSchedulingAction.SEQUENTIAL:
        return SchedulingOutcome.PROCEED_SEQUENTIAL
    if action == ReleaseSchedulingAction.STACKED:
        return SchedulingOutcome.STACKED_BRANCHES
    if action == ReleaseSchedulingAction.REPLAN:
        return SchedulingOutcome.REPLAN
    return SchedulingOutcome.STOP


def _release_scheduling_fallback_plan(action: ReleaseSchedulingAction) -> str:
    if action == ReleaseSchedulingAction.SEQUENTIAL:
        return "Rerun overlap analysis and verification if the selected source scope changes before re-enabling parallel execution."
    if action == ReleaseSchedulingAction.PARALLEL:
        return "Rerun the dependency graph and overlap analysis if new file overlap appears before execution resumes."
    if action == ReleaseSchedulingAction.STACKED:
        return "Stacked branch scheduling is not yet implemented; re-slice the release into sequential or parallel tasks."
    if action == ReleaseSchedulingAction.REPLAN:
        return "Replan the release contract package before retrying execution."
    return "Stop and inspect the release decision evidence before resuming execution."


def _release_scheduling_validators_to_rerun(action: ReleaseSchedulingAction) -> list[str]:
    if action == ReleaseSchedulingAction.SEQUENTIAL:
        return ["overlap_report", "execution_dag", "verification"]
    if action == ReleaseSchedulingAction.PARALLEL:
        return ["execution_dag", "verification"]
    return ["overlap_report", "verification"]


def _build_release_scheduling_decision(
    *,
    config_repo_path: Path,
    base_branch: str,
    release_id: str,
    execution_mode: str,
    selected_contracts: list[Path],
    selected_tasks: list[TaskContract],
    overlap_report: ReleaseOverlapReport,
    overlap_report_path: Path,
    dependencies: dict[str, list[str]],
    progress: Callable[[str], None] | None = None,
) -> ReleaseSchedulingDecision:
    selected_action = _release_scheduling_action_for_execution_mode(
        execution_mode=execution_mode,
        overlap_report=overlap_report,
    )
    staleness_inputs = _release_scheduling_staleness_inputs(
        config_repo_path=config_repo_path,
        base_branch=base_branch,
        release_id=release_id,
        execution_mode=execution_mode,
        selected_contracts=selected_contracts,
        selected_tasks=selected_tasks,
        overlap_report=overlap_report,
    )
    evidence_paths = [overlap_report_path.resolve(), *[path.resolve() for path in selected_contracts]]
    if selected_action == ReleaseSchedulingAction.SEQUENTIAL:
        if overlap_report.findings:
            rationale = "Normal source overlap is serialized for safety."
        else:
            rationale = "The release was requested in sequential mode."
        if dependencies:
            rationale = f"{rationale} Explicit dependencies are preserved in the release DAG."
    else:
        rationale = "Independent release tasks can proceed in parallel."
    return ReleaseSchedulingDecision.model_validate(
        {
            "decision_id": f"{release_id}__scheduling",
            "release_id": release_id,
            "decided_at": datetime.now(UTC),
            "decided_by": "deterministic_kernel",
            "rationale": rationale,
            "evidence_paths": evidence_paths,
            "decision_type": SupervisorDecisionType.RELEASE_SCHEDULING,
            "risk_level": (
                "moderate"
                if overlap_report.findings
                else "low"
            ),
            "overlap_findings": [finding.pattern for finding in overlap_report.findings],
            "selected_action": selected_action,
            "outcome": _release_scheduling_outcome_for_action(selected_action),
            "fallback_plan": _release_scheduling_fallback_plan(selected_action),
            "validators_to_rerun": _release_scheduling_validators_to_rerun(selected_action),
            "staleness_inputs": staleness_inputs.model_dump(mode="json"),
        }
    )


def _load_or_build_release_scheduling_decision(
    *,
    release_root: Path,
    release_id: str,
    config: "ProjectConfig",
    base_branch: str,
    execution_mode: str,
    selected_contracts: list[Path],
    selected_tasks: list[TaskContract],
    overlap_report: ReleaseOverlapReport,
    overlap_report_path: Path,
    dependencies: dict[str, list[str]],
    progress: Callable[[str], None] | None = None,
) -> ReleaseSchedulingDecision:
    decision_path = _release_scheduling_decision_path(release_root, release_id)
    current_staleness_inputs = _release_scheduling_staleness_inputs(
        config_repo_path=config.repo_path,
        base_branch=base_branch,
        release_id=release_id,
        execution_mode=execution_mode,
        selected_contracts=selected_contracts,
        selected_tasks=selected_tasks,
        overlap_report=overlap_report,
    )
    if decision_path.exists():
        try:
            loaded, legacy_warning_loaded = _load_supervisor_decision_artifact_silencing_legacy_warning(decision_path)
            if legacy_warning_loaded:
                _report(
                    progress,
                    "event=legacy_supervisor_decision_artifact_loaded "
                    f"type={SupervisorDecisionType.RELEASE_SCHEDULING.value} path={decision_path}",
                )
        except Exception as error:  # noqa: BLE001 - bounded normalization handles typed reload safety.
            loaded = _normalize_release_scheduling_model_output_if_needed(
                release_id=release_id,
                release_root=release_root,
                decision_path=decision_path,
                current_staleness_inputs=current_staleness_inputs,
                load_error=error,
            )
            if loaded is None:
                raise
        if not isinstance(loaded, ReleaseSchedulingDecision):
            raise ValueError(
                f"release scheduling decision artifact has unsupported type: {loaded.decision_type}"
            )
        if loaded.staleness_inputs != current_staleness_inputs:
            raise ValueError(
                f"release scheduling decision artifact is stale for release {release_id}: {decision_path}"
            )
        if loaded.selected_action not in {ReleaseSchedulingAction.SEQUENTIAL, ReleaseSchedulingAction.PARALLEL}:
            raise ValueError(f"unsupported release scheduling action: {loaded.selected_action.value}")
        return loaded

    decision = _build_release_scheduling_decision(
        config_repo_path=config.repo_path,
        base_branch=base_branch,
        release_id=release_id,
        execution_mode=execution_mode,
        selected_contracts=selected_contracts,
        selected_tasks=selected_tasks,
        overlap_report=overlap_report,
        overlap_report_path=overlap_report_path,
        dependencies=dependencies,
    )
    written_path = write_supervisor_decision_artifact(
        release_bundle_path=release_root,
        decision=decision,
    )
    if written_path != decision_path:
        raise RuntimeError(
            f"release scheduling decision artifact was written to unexpected path: {written_path}"
        )
    loaded = load_supervisor_decision_artifact(decision_path)
    if not isinstance(loaded, ReleaseSchedulingDecision):
        raise ValueError(
            f"release scheduling decision artifact has unsupported type: {loaded.decision_type}"
        )
    if loaded.staleness_inputs != current_staleness_inputs:
        raise ValueError(
            f"release scheduling decision artifact is stale for release {release_id}: {decision_path}"
        )
    if loaded.selected_action not in {ReleaseSchedulingAction.SEQUENTIAL, ReleaseSchedulingAction.PARALLEL}:
        raise ValueError(f"unsupported release scheduling action: {loaded.selected_action.value}")
    return loaded


def _normalize_release_scheduling_model_output_if_needed(
    *,
    release_id: str,
    release_root: Path,
    decision_path: Path,
    current_staleness_inputs: ReleaseSchedulingStalenessInputs,
    load_error: Exception,
) -> ReleaseSchedulingDecision | None:
    raw_text = decision_path.read_text(encoding="utf-8")
    raw_payload = _extract_json_object_from_text(raw_text)
    raw_paths = [decision_path]
    if raw_payload is None:
        _write_release_scheduling_output_normalization_decision(
            release_id=release_id,
            release_root=release_root,
            raw_paths=raw_paths,
            validation_errors=_model_output_validation_errors_from_exception(load_error),
            selected_action=ModelOutputNormalizationAction.REFUSE,
            outcome=ModelOutputNormalizationOutcome.REFUSED_AND_STOP,
            normalized_artifact_path=None,
            refusal_reason="Release scheduling decision artifact was not parseable JSON.",
        )
        return None

    validation_errors = _release_scheduling_validation_errors(raw_payload)
    normalized_payload, refusal_reason = _bounded_normalize_release_scheduling_payload(
        raw_payload=raw_payload,
        release_id=release_id,
        current_staleness_inputs=current_staleness_inputs,
    )
    if normalized_payload is None:
        _write_release_scheduling_output_normalization_decision(
            release_id=release_id,
            release_root=release_root,
            raw_paths=raw_paths,
            validation_errors=validation_errors,
            selected_action=ModelOutputNormalizationAction.REFUSE,
            outcome=ModelOutputNormalizationOutcome.REFUSED_AND_STOP,
            normalized_artifact_path=None,
            refusal_reason=refusal_reason or "Release scheduling normalization refused by bounded policy.",
        )
        return None

    try:
        normalized_decision = ReleaseSchedulingDecision.model_validate(normalized_payload)
        if normalized_decision.staleness_inputs != current_staleness_inputs:
            raise ValueError("normalized release scheduling decision artifact remained stale")
    except Exception as error:  # noqa: BLE001 - refusal evidence is persisted.
        _write_release_scheduling_output_normalization_decision(
            release_id=release_id,
            release_root=release_root,
            raw_paths=raw_paths,
            validation_errors=validation_errors,
            selected_action=ModelOutputNormalizationAction.REFUSE,
            outcome=ModelOutputNormalizationOutcome.REFUSED_AND_STOP,
            normalized_artifact_path=None,
            refusal_reason=f"Normalized release scheduling output remained invalid: {error}",
        )
        return None

    decision_path.write_text(
        json.dumps(normalized_decision.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    loaded = load_supervisor_decision_artifact(decision_path)
    if not isinstance(loaded, ReleaseSchedulingDecision):
        _write_release_scheduling_output_normalization_decision(
            release_id=release_id,
            release_root=release_root,
            raw_paths=raw_paths,
            validation_errors=validation_errors,
            selected_action=ModelOutputNormalizationAction.REFUSE,
            outcome=ModelOutputNormalizationOutcome.REFUSED_AND_STOP,
            normalized_artifact_path=decision_path,
            refusal_reason="Normalized release scheduling artifact reloaded to an unsupported decision type.",
        )
        return None

    _write_release_scheduling_output_normalization_decision(
        release_id=release_id,
        release_root=release_root,
        raw_paths=raw_paths,
        validation_errors=validation_errors,
        selected_action=ModelOutputNormalizationAction.APPLY_NORMALIZATION,
        outcome=ModelOutputNormalizationOutcome.NORMALIZED_AND_RETRY,
        normalized_artifact_path=decision_path,
        refusal_reason=None,
    )
    return loaded


def _release_scheduling_validation_errors(candidate: object) -> list[dict[str, str]]:
    try:
        ReleaseSchedulingDecision.model_validate(candidate)
        return []
    except Exception as error:  # noqa: BLE001 - validation errors are best-effort evidence.
        return _model_output_validation_errors_from_exception(error)


def _model_output_validation_errors_from_exception(error: Exception) -> list[dict[str, str]]:
    errors_fn = getattr(error, "errors", None)
    if not callable(errors_fn):
        return []
    payload: list[dict[str, str]] = []
    for item in errors_fn():
        loc = item.get("loc", ())
        payload.append(
            {
                "field": ".".join(str(token) for token in loc) if loc else "<root>",
                "message": str(item.get("msg", "validation failed")),
                "error_type": str(item.get("type", "value_error")),
            }
        )
    return payload


def _bounded_normalize_release_scheduling_payload(
    *,
    raw_payload: dict[str, object],
    release_id: str,
    current_staleness_inputs: ReleaseSchedulingStalenessInputs,
) -> tuple[dict[str, object] | None, str | None]:
    candidate = dict(raw_payload)
    nested_candidate = None
    for key in ("decision", "scheduling_decision", "release_scheduling", "result", "output"):
        value = raw_payload.get(key)
        if isinstance(value, dict):
            nested_candidate = value
            break
    if nested_candidate is not None:
        if _payload_looks_like_release_scheduling(candidate) and _payload_looks_like_release_scheduling(nested_candidate):
            if _release_scheduling_semantics_fingerprint(candidate) != _release_scheduling_semantics_fingerprint(
                nested_candidate
            ):
                return None, "Supervisor wrapper and nested release scheduling decision disagree on selected action semantics."
        candidate = dict(nested_candidate)

    if not _payload_looks_like_release_scheduling(candidate):
        return None, "Supervisor output did not include a recognizable release scheduling decision payload."

    normalized = dict(candidate)
    if "selected_action" not in normalized and "action" in normalized:
        normalized["selected_action"] = normalized["action"]
    if "decision_type" not in normalized:
        normalized["decision_type"] = SupervisorDecisionType.RELEASE_SCHEDULING.value
    if "release_id" not in normalized:
        normalized["release_id"] = release_id
    if "decision_id" not in normalized:
        normalized["decision_id"] = f"{release_id}__scheduling"
    if "decided_by" not in normalized:
        normalized["decided_by"] = "run_release_scheduling_normalizer"
    if "decided_at" not in normalized:
        normalized["decided_at"] = datetime.now(UTC).isoformat()
    if "rationale" not in normalized:
        normalized["rationale"] = "Bounded normalization repaired wrapper/schema drift for release scheduling output."
    if "overlap_findings" not in normalized:
        normalized["overlap_findings"] = []
    if "risk_level" not in normalized:
        normalized["risk_level"] = DecisionRiskLevel.MODERATE.value
    if "evidence_paths" not in normalized or not isinstance(normalized.get("evidence_paths"), list):
        normalized["evidence_paths"] = []

    selected_action = normalized.get("selected_action")
    if not isinstance(selected_action, str):
        return None, "Release scheduling selected_action was missing or invalid."
    if selected_action not in {ReleaseSchedulingAction.SEQUENTIAL.value, ReleaseSchedulingAction.PARALLEL.value}:
        return None, f"Unsupported release scheduling action for bounded normalization: {selected_action}"

    action_enum = ReleaseSchedulingAction(selected_action)
    normalized["outcome"] = _release_scheduling_outcome_for_action(action_enum).value
    normalized["fallback_plan"] = _release_scheduling_fallback_plan(action_enum)
    normalized["validators_to_rerun"] = _release_scheduling_validators_to_rerun(action_enum)
    normalized["staleness_inputs"] = current_staleness_inputs.model_dump(mode="json")
    return normalized, None


def _payload_looks_like_release_scheduling(payload: dict[str, object]) -> bool:
    return "selected_action" in payload or "action" in payload


def _release_scheduling_semantics_fingerprint(payload: dict[str, object]) -> dict[str, object]:
    return {
        "release_id": payload.get("release_id"),
        "selected_action": payload.get("selected_action", payload.get("action")),
        "outcome": payload.get("outcome"),
        "overlap_findings": payload.get("overlap_findings"),
    }


def _write_release_scheduling_output_normalization_decision(
    *,
    release_id: str,
    release_root: Path,
    raw_paths: list[Path],
    validation_errors: list[dict[str, str]],
    selected_action: ModelOutputNormalizationAction,
    outcome: ModelOutputNormalizationOutcome,
    normalized_artifact_path: Path | None,
    refusal_reason: str | None,
) -> Path:
    decision = ModelOutputNormalizationDecision.model_validate(
        {
            "decision_type": SupervisorDecisionType.MODEL_OUTPUT_NORMALIZATION,
            "decision_id": f"{release_id}__release_scheduling_output",
            "release_id": release_id,
            "decided_at": datetime.now(UTC),
            "decided_by": "run_release_scheduling_loader",
            "rationale": (
                "Applied bounded normalization to release scheduling output and reran strict supervisor decision validators."
                if outcome == ModelOutputNormalizationOutcome.NORMALIZED_AND_RETRY
                else "Refused release scheduling output normalization because bounded policy could not guarantee safe semantics."
            ),
            "evidence_paths": [str(path.resolve()) for path in raw_paths if path.exists()],
            "risk_level": DecisionRiskLevel.MODERATE,
            "raw_artifact_paths": [str(path.resolve()) for path in raw_paths if path.exists()],
            "validation_errors": [
                ModelOutputValidationError.model_validate(item).model_dump(mode="json")
                for item in validation_errors
            ],
            "selected_action": selected_action.value,
            "outcome": outcome.value,
            "fallback_plan": "Keep deterministic scheduling selection behavior and require bounded rerun for invalid supervisor artifacts.",
            "validators_to_rerun": ["ReleaseSchedulingDecision", "staleness_inputs", "selected_action"],
            "normalized_artifact_path": str(normalized_artifact_path.resolve()) if normalized_artifact_path else None,
            "refusal_reason": refusal_reason,
        }
    )
    return write_supervisor_decision_artifact(release_bundle_path=release_root, decision=decision)


def _completed_release_task_ids(
    *,
    runs_dir: Path,
    release_id: str,
    integration_branch: str,
) -> set[str]:
    completed: set[str] = set()
    for summary_path in sorted(runs_dir.glob(f"*_{release_id}_release/release_summary.json")):
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if summary.get("release_id") != release_id:
            continue
        if summary.get("integration_branch") != integration_branch:
            continue
        for task in summary.get("tasks", []):
            if not isinstance(task, dict):
                continue
            if task.get("decision") != Decision.ACCEPTED:
                continue
            task_id = task.get("task_id")
            if isinstance(task_id, str) and task_id:
                if task.get("merged"):
                    completed.add(task_id)
                    continue
                bundle_path = task.get("bundle_path")
                if isinstance(bundle_path, str) and bundle_path:
                    if _bundle_has_no_changed_files(Path(bundle_path)):
                        completed.add(task_id)
    return completed


def _bundle_has_no_changed_files(bundle_path: Path) -> bool:
    changed_files_path = bundle_path / "changed_files.txt"
    if not changed_files_path.exists():
        return False
    lines = [line.strip() for line in changed_files_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return not lines


def _release_dependency_map(
    tasks: list[TaskContract],
    overlap_report: ReleaseOverlapReport,
    *,
    completed_task_ids: set[str] | None = None,
) -> dict[str, list[str]]:
    task_ids = {task.task_id for task in tasks}
    completed_task_ids = completed_task_ids or set()
    dependencies: dict[str, set[str]] = {task.task_id: set(task.depends_on) for task in tasks}
    for task in tasks:
        unknown = sorted(set(task.depends_on) - task_ids - completed_task_ids)
        if unknown:
            raise ValueError(
                f"task {task.task_id} depends on unknown release task(s): {', '.join(unknown)}"
            )
    for finding in overlap_report.findings:
        if finding.severity == "minor":
            if finding.second_task_id not in dependencies:
                continue
            dependencies[finding.second_task_id].add(finding.first_task_id)
    return {
        task_id: sorted(values - completed_task_ids)
        for task_id, values in dependencies.items()
        if values - completed_task_ids
    }


def _ordered_release_task_inputs(
    *,
    task_inputs: list[tuple[Path, TaskContract]],
    dependencies: dict[str, list[str]],
    completed_task_ids: set[str],
) -> list[tuple[Path, TaskContract]]:
    task_ids = [task.task_id for _, task in task_inputs]
    task_ids_set = set(task_ids)
    remaining_dependencies: dict[str, set[str]] = {}
    for task_id in task_ids:
        dependency_set = set(dependencies.get(task_id, [])) - completed_task_ids
        unknown = sorted(dependency_set - task_ids_set)
        if unknown:
            raise ValueError(
                f"task {task_id} depends on unknown release task(s): {', '.join(unknown)}"
            )
        remaining_dependencies[task_id] = dependency_set

    ready = [task_id for task_id in task_ids if not remaining_dependencies[task_id]]
    ordered: list[tuple[Path, TaskContract]] = []
    emitted: set[str] = set()
    while ready:
        task_id = ready.pop(0)
        contract_path, task = next(item for item in task_inputs if item[1].task_id == task_id)
        ordered.append((contract_path, task))
        emitted.add(task_id)
        for remaining_task_id, task_dependencies in remaining_dependencies.items():
            if task_id not in task_dependencies:
                continue
            task_dependencies.remove(task_id)
            if not task_dependencies and remaining_task_id not in emitted and remaining_task_id not in ready:
                ready.append(remaining_task_id)

    if len(ordered) != len(task_inputs):
        blocked = ", ".join(sorted(task_id for task_id in task_ids_set if task_id not in emitted))
        raise ValueError(f"release execution DAG has unsatisfied dependencies: {blocked}")
    return ordered


def _overlap_severity(
    first: str,
    second: str,
    *,
    unsafe_overlap_paths: list[str],
) -> str | None:
    if not _patterns_overlap(first, second):
        return None
    if _is_hard_unsafe_overlap(first, second, unsafe_overlap_paths=unsafe_overlap_paths):
        return "blocking"
    return "minor"


def _is_hard_unsafe_overlap(first: str, second: str, *, unsafe_overlap_paths: list[str]) -> bool:
    return any(
        (
            _is_configured_unsafe_overlap(first, second, unsafe_pattern)
            for unsafe_pattern in unsafe_overlap_paths
        )
    ) or any(
        (
            _is_forbidden_overlap_path(first),
            _is_forbidden_overlap_path(second),
            _is_out_of_scope_overlap_path(first),
            _is_out_of_scope_overlap_path(second),
            _is_generated_artifact_path(first),
            _is_generated_artifact_path(second),
            _is_lockfile_path(first),
            _is_lockfile_path(second),
            _is_migration_path(first),
            _is_migration_path(second),
            _is_destructive_script_path(first),
            _is_destructive_script_path(second),
        )
    )


def _is_configured_unsafe_overlap(first: str, second: str, unsafe_pattern: str) -> bool:
    return _patterns_overlap(first, unsafe_pattern) or _patterns_overlap(second, unsafe_pattern)


def _is_forbidden_overlap_path(pattern: str) -> bool:
    normalized = pattern.strip().lstrip("./")
    return normalized == ".git" or normalized.startswith(".git/")


def _is_out_of_scope_overlap_path(pattern: str) -> bool:
    normalized = pattern.strip()
    return normalized in {"*", "**", "**/*", "./**", "./**/*", "/**", "/**/*"}


def _is_generated_artifact_path(pattern: str) -> bool:
    normalized = pattern.strip().lstrip("./").lower()
    if normalized.startswith("dist/") or normalized.startswith("build/") or normalized.startswith("runs/"):
        return True
    if normalized.endswith(".min.js") or normalized.endswith(".generated.py"):
        return True
    return "/generated/" in normalized or normalized.startswith("generated/")


def _is_lockfile_path(pattern: str) -> bool:
    normalized = pattern.strip().lstrip("./")
    lockfile_names = {
        "poetry.lock",
        "uv.lock",
        "pdm.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.lock",
        "Gemfile.lock",
        "composer.lock",
    }
    return Path(normalized).name in lockfile_names


def _is_migration_path(pattern: str) -> bool:
    normalized = pattern.strip().lstrip("./").lower()
    return (
        "/migrations/" in normalized
        or normalized.startswith("migrations/")
        or normalized.endswith("/migrations")
        or normalized.startswith("alembic/versions/")
    )


def _is_destructive_script_path(pattern: str) -> bool:
    normalized = pattern.strip().lstrip("./").lower()
    path = Path(normalized)
    destructive_tokens = {"destroy", "destruct", "delete", "wipe", "nuke", "reset", "drop", "purge"}
    return path.parts and path.parts[0] in {"scripts", "script", "bin"} and any(
        token in path.stem for token in destructive_tokens
    )


def _patterns_overlap(first: str, second: str) -> bool:
    if first == second:
        return True
    if first == "**" or second == "**":
        return True
    first_prefix = _glob_prefix(first)
    second_prefix = _glob_prefix(second)
    if first_prefix and second_prefix:
        return first_prefix.startswith(second_prefix) or second_prefix.startswith(first_prefix)
    return fnmatch(first, second) or fnmatch(second, first)


def _glob_prefix(pattern: str) -> str:
    wildcard_index = min([index for index in [pattern.find("*"), pattern.find("?")] if index >= 0], default=-1)
    if wildcard_index < 0:
        return pattern
    return pattern[:wildcard_index].rstrip("/")


def _report(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _should_preserve_task_branch(result: TaskRunResult) -> bool:
    if result.finalize is None:
        return result.decision.decision == Decision.ACCEPTED
    if result.finalize.error is not None:
        return True
    return bool(result.finalize.commit_hash and not result.finalize.merged)


def _should_preserve_task_worktree(result: TaskRunResult) -> bool:
    if result.finalize is None:
        return result.decision.decision == Decision.ACCEPTED
    return result.finalize.error is not None


def _multiplexed_progress(
    progress: Callable[[str], None] | None,
    log_path: Path,
    raw_log_path: Path,
) -> Callable[[str], None]:
    lock = threading.Lock()
    formatter = _HumanLogFormatter()

    def report(message: str) -> None:
        timestamp = datetime.now(UTC).isoformat()
        raw_line = f"{timestamp} {message}\n"
        display_messages = formatter.format(message)
        with lock:
            raw_log_path.parent.mkdir(parents=True, exist_ok=True)
            with raw_log_path.open("a", encoding="utf-8") as file:
                file.write(raw_line)
            if display_messages:
                with log_path.open("a", encoding="utf-8") as file:
                    for display_message in display_messages:
                        file.write(f"{timestamp} {display_message}\n")
        if progress is not None:
            for display_message in display_messages:
                progress(display_message)

    return report


class _HumanLogFormatter:
    def __init__(self) -> None:
        self._active_worker_sections: dict[tuple[str, str], str] = {}

    def format(self, message: str) -> list[str]:
        if message.startswith("event="):
            return [_display_event_message(message)]
        if message.startswith("agent "):
            return self._format_agent_message(message)
        return [_style(f"ℹ️  {message}", "dim")]

    def _format_agent_message(self, message: str) -> list[str]:
        metadata, separator, content = message.partition(" | ")
        if not separator:
            return []
        fields = _agent_fields(metadata)
        if fields.get("stream") != "stdout":
            return []
        task = fields.get("task", "?")
        attempt = fields.get("attempt", "?")
        key = (task, attempt)
        text = _clean_worker_line(content)
        if not text:
            return []

        heading = _worker_heading(text)
        if heading is not None:
            self._active_worker_sections[key] = heading
            return [_style(f"📝 {task} worker summary: {heading}", "cyan")]

        section = self._active_worker_sections.get(key)
        if section is not None:
            if text.startswith(("-", "*")) or text.startswith("`") or "passed" in text.lower() or "failed" in text.lower():
                return [f"   {_style(_shorten_worker_paths(text), 'dim')}"]
            return [f"   {_shorten_worker_paths(text)}"]

        if _looks_like_worker_summary(text):
            return [_style(f"🤖 {task}: {_truncate_log_line(text, 180)}", "cyan")]
        return []


def _display_event_message(message: str) -> str:
    event = _event_fields(message)
    name = event.get("event", "event")
    if name == "release_started":
        return _style(
            f"🚀 Release {event.get('release')} started: {event.get('tasks')} task(s), mode={event.get('mode')}, run={event.get('run_id')}",
            "bold",
        )
    if name == "release_logs":
        return f"📡 Watching: {_style(str(event.get('log')), 'cyan')}  {_style('(raw audit: ' + str(event.get('raw_log')) + ')', 'dim')}"
    if name == "integration_branch":
        return f"🌿 Integration branch ready: {_style(str(event.get('branch')), 'cyan')} from {event.get('base')}"
    if name == "execution_dag":
        return f"🕸️ Execution DAG: {_style(_compact_value(str(event.get('dependencies'))), 'dim')}"
    if name == "overlap_risk_report":
        severity = event.get("severity", "soft")
        detail = "scheduler will serialize risky overlap" if severity != "blocking" else "hard gate remains authoritative"
        return f"🧩 Overlap-risk report: {event.get('count')} finding(s)  {_style(detail, 'dim')} {_style(str(event.get('path')), 'dim')}"
    if name == "scheduling_decision":
        return f"🧭 Release scheduling decision: {event.get('action')} -> {event.get('outcome')}  {_style(str(event.get('path')), 'dim')}"
    if name == "admission_repair_records":
        return f"🧾 Admission-repair records: {_style(str(event.get('path')), 'dim')}"
    if name == "admission_repair_attempt":
        return (
            "🛠️ Admission repair attempt "
            f"{event.get('attempt')}: task={event.get('task')} "
            f"action={event.get('action')} outcome={event.get('outcome')}"
        )
    if name == "task_started":
        return _style(
            f"🧭 Task {event.get('index')}/{event.get('total')} {event.get('task')}: {event.get('title')} [{event.get('type')}, budget {event.get('budget')}]",
            "bold",
        )
    if name == "task_submitted":
        return f"🧭 Task submitted {event.get('task')}: {event.get('title')} [{event.get('type')}, budget {event.get('budget')}]"
    if name == "task_objective":
        return f"🎯 {event.get('task')} objective: {_truncate_log_line(str(event.get('objective')), 260)}"
    if name == "task_scope":
        return f"🗂️ {event.get('task')} scope: {_compact_value(str(event.get('allowed')))}"
    if name == "task_run_created":
        return f"🧪 {event.get('task')} run: {_style(str(event.get('run_id')), 'dim')}"
    if name == "worktree_created":
        return f"🌱 {event.get('task')} worktree created: {_style(_short_path(str(event.get('path'))), 'dim')}"
    if name == "prompt_build_started":
        return f"🧵 {event.get('task')} building executor prompt"
    if name == "context_loaded":
        return f"📚 {event.get('task')} context loaded: {event.get('sections')} section(s), {event.get('chars')} chars"
    if name == "executor_attempt_started":
        return f"🤖 {event.get('task')} worker started: attempt {event.get('attempt')}/{event.get('total')} on {_style(str(event.get('model')), 'cyan')}"
    if name == "executor_heartbeat":
        elapsed = _format_duration(int(event.get("elapsed_seconds", "0")))
        return _style(
            f"🤖 {event.get('task')} still working after {elapsed} on {event.get('model')}. Inspect raw log before poking the goblin.",
            "yellow",
        )
    if name == "executor_attempt_finished":
        style = "green" if event.get("exit_code") == "0" else "yellow"
        return _style(f"{_status_icon(str(event.get('exit_code')))} {event.get('task')} worker attempt {event.get('attempt')} finished: exit={event.get('exit_code')}", style)
    if name == "executor_finished":
        style = "green" if event.get("exit_code") == "0" else "red"
        return _style(f"{_status_icon(str(event.get('exit_code')))} {event.get('task')} worker finished: exit={event.get('exit_code')}", style)
    if name == "verification_started":
        return f"🔎 {event.get('task')} verification started: {event.get('commands')} command(s)"
    if name == "verification_finished":
        style = "green" if str(event.get("exit_codes", "")).replace(",", "").strip("0") == "" else "red"
        return _style(f"{_status_icon(str(event.get('exit_codes')))} {event.get('task')} verification finished: exit_codes={event.get('exit_codes')}", style)
    if name == "evidence_collection_started":
        return f"🧾 {event.get('task')} collecting evidence"
    if name == "review_decision":
        decision = str(event.get("decision"))
        line = f"{_status_icon(decision)} {event.get('task')} review: {decision} - {event.get('rationale')}"
        if decision != "accepted":
            line += _style("  ⚠️ Human should inspect evidence before continuing.", "yellow")
        return _style(line, _decision_style(decision))
    if name == "task_finalization_started":
        return f"📦 {event.get('task')} finalizing accepted changes"
    if name == "task_committed":
        return _style(f"✅ {event.get('task')} committed: {str(event.get('commit'))[:12]}", "green")
    if name == "task_merged":
        return _style(f"🔀 {event.get('task')} merged into {event.get('target')}", "green")
    if name == "task_pushed":
        return _style(f"📤 {event.get('task')} pushed: {event.get('branch')}", "green")
    if name == "task_completed":
        return _style(f"✅ {event.get('task')} completed: {event.get('decision')}", _decision_style(str(event.get("decision"))))
    if name == "failure_diagnosis":
        return _style(f"🩺 {event.get('task')} failure diagnosis: {event.get('category')}", "yellow")
    if name == "conflict_repair_started":
        return _style(f"🧯 {event.get('task')} conflict repair started: {event.get('files')} file(s)", "yellow")
    if name == "release_decision":
        return _style(f"{_status_icon(str(event.get('decision')))} Release decision: {event.get('decision')}", _decision_style(str(event.get("decision"))))
    if name == "release_review":
        return f"🧑‍⚖️ Release review: {_style(str(event.get('path')), 'dim')}"
    if name == "release_metrics":
        return f"📊 Release metrics: {_style(str(event.get('path')), 'dim')}"
    if name == "release_budget":
        return f"💰 Release budget: {_style(str(event.get('path')), 'dim')}"
    if name == "release_tuning":
        return f"🛠️ Release tuning: {_style(str(event.get('path')), 'dim')}"
    if name == "release_budget_exceeded":
        return _style(f"⚠️ Release budget exceeded: {_compact_value(str(event.get('violations')))}. Stop and inspect budget/tuning artifacts.", "yellow")
    if name == "release_pushed":
        return _style(f"📤 Release pushed: {event.get('branch')}", "green")
    if name == "release_merged":
        return _style(f"🔀 Release merged into {event.get('target')}", "green")
    return _style(f"ℹ️  {message}", "dim")


def _event_fields(message: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    try:
        parts = shlex.split(message)
    except ValueError:
        parts = message.split()
    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[key] = value
    return fields


def _agent_fields(metadata: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in metadata.split():
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key] = value
    return fields


ANSI_STYLES = {
    "bold": "\033[1m",
    "cyan": "\033[36m",
    "dim": "\033[2m",
    "green": "\033[32m",
    "red": "\033[31m",
    "yellow": "\033[33m",
}
ANSI_RESET = "\033[0m"


def _style(text: str, style: str) -> str:
    prefix = ANSI_STYLES.get(style)
    if prefix is None:
        return text
    return f"{prefix}{text}{ANSI_RESET}"


def _decision_style(value: str) -> str:
    if value in {"accepted", "0", "None"}:
        return "green"
    if value in {"failed", "escalated"}:
        return "red"
    return "yellow"


def _status_icon(value: str) -> str:
    normalized = value.lower()
    if normalized in {"accepted", "0"} or set(normalized.replace(",", "")) <= {"0"}:
        return "✅"
    if normalized in {"failed", "escalated"}:
        return "🛑"
    if normalized in {"needs_revision", "needs-revision"}:
        return "⚠️"
    return "⚠️"


def _short_path(value: str) -> str:
    marker = "/auto_develop/"
    if marker in value:
        return "…/auto_develop/" + value.split(marker, 1)[1]
    return value


def _shorten_worker_paths(value: str) -> str:
    value = value.replace("](/Users/fuhrer/Desktop/auto_develop/worktrees/", "](…/worktrees/")
    value = value.replace("/Users/fuhrer/Desktop/auto_develop/worktrees/", "…/worktrees/")
    value = value.replace("/Users/fuhrer/Desktop/auto_develop/main/", "…/main/")
    return value


def _compact_value(value: str, limit: int = 220) -> str:
    compact = value.replace("\\n", " ").replace("[", "").replace("]", "").replace('"', "")
    compact = " ".join(compact.split())
    return _truncate_log_line(compact, limit)


def _format_duration(seconds: int) -> str:
    minutes, remainder = divmod(max(0, seconds), 60)
    if minutes:
        return f"{minutes}m {remainder}s"
    return f"{remainder}s"


USEFUL_WORKER_HEADINGS = {
    "changed files": "Files changed",
    "files changed": "Files changed",
    "what changed": "What changed",
    "result": "Result",
    "verification": "Verification",
    "verification commands run": "Verification",
    "verification result": "Verification result",
    "risks": "Risks",
    "risks / follow-up": "Risks / follow-up",
    "follow-up": "Follow-up",
    "notes": "Notes",
    "documentation review summary": "Documentation review summary",
}


def _worker_heading(line: str) -> str | None:
    normalized = line.strip().strip("*").strip(":").lower()
    return USEFUL_WORKER_HEADINGS.get(normalized)


def _looks_like_worker_summary(line: str) -> bool:
    lowered = line.lower()
    return lowered.startswith(("implemented ", "updated ", "added ", "fixed ", "created "))


def _clean_worker_line(line: str) -> str:
    text = line.strip()
    if not text:
        return ""
    if text.startswith("```"):
        return ""
    if text.startswith(("exec ", "codex ", "apply patch", "/bin/")):
        return ""
    if " WARN codex_" in text or "codex_core_plugins" in text or "codex_core_skills" in text:
        return ""
    return text


def _truncate_log_line(message: str, limit: int = 500) -> str:
    if len(message) <= limit:
        return message
    return message[: limit - 15] + "... [truncated]"
