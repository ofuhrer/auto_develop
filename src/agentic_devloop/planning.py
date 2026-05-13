from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from agentic_devloop.budget import reserve_strong_model_call
from agentic_devloop.config import load_project_config
from agentic_devloop.contracts import normalize_contract_request, normalize_task_contract_payload
from agentic_devloop.execution_strategy import (
    ExecutionStrategyAction as SelectorExecutionStrategyAction,
    ExecutionStrategySelection,
    ExecutionStrategySelectorInput,
    select_execution_strategy,
)
from agentic_devloop.models import (
    ContractNormalizationRequest,
    ContractPlan,
    GeneratedContract,
    ProjectConfig,
    ReleaseObjective,
    StrictModel,
    TaskContract,
)
from agentic_devloop.planner_backend import PlannerBackendResult
from agentic_devloop.runtime_supervisor import RuntimeSupervisor, RuntimeSupervisorApplierStopKind
from agentic_devloop.supervisor_decisions import (
    DecisionRiskLevel,
    ExecutionStrategyAction as SupervisorExecutionStrategyAction,
    ExecutionStrategyDecision,
    ExecutionStrategyOutcome,
    SupervisorDecisionType,
    write_supervisor_decision_artifact,
)
from agentic_devloop.yaml_io import load_yaml_model, write_yaml_model


@dataclass(frozen=True)
class ContractPlanResult:
    release_id: str
    plan_path: Path
    plan: ContractPlan
    written_contract_paths: list[Path] = field(default_factory=list)
    execution_strategy_selection: ExecutionStrategySelection | None = None
    execution_strategy_selection_path: Path | None = None
    supervisor_decision_path: Path | None = None
    one_shot_execution_input_path: Path | None = None


class OneShotExecutionInput(StrictModel):
    schema_version: str = "1.0"
    release_id: str
    created_at: datetime
    created_by: str

    objective_path: Path
    objective: ReleaseObjective
    project_id: str | None = None
    config_dir: Path | None = None
    budget: dict[str, Any] | None = None
    verification_profiles: dict[str, Any] | None = None

    scope: dict[str, Any]
    evidence_requirements: list[str]
    stop_conditions: list[str]
    selector_inputs: ExecutionStrategySelectorInput
    selector_selection: ExecutionStrategySelection


@dataclass(frozen=True)
class PlannerNormalizationStopEvidence:
    kind: RuntimeSupervisorApplierStopKind
    reason: str


class PlannerNormalizationError(ValueError):
    def __init__(self, message: str, *, stop_evidence: PlannerNormalizationStopEvidence) -> None:
        super().__init__(message)
        self.stop_evidence = stop_evidence


class PlannerBackend(Protocol):
    def generate(
        self,
        *,
        prompt: str,
        objective: ReleaseObjective,
        existing_contracts: list[TaskContract],
        model: str,
    ) -> str | dict[str, Any] | ContractPlan | PlannerBackendResult:
        ...


def make_plan_id(release_id: str, now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{release_id}_plan"


def plan_release_contracts(
    *,
    objective_path: Path,
    contracts_dir: Path = Path("contracts"),
    runs_dir: Path = Path("runs"),
    write_contracts_dir: Path | None = None,
    mode: str = "deterministic",
    project_id: str | None = None,
    config_dir: Path = Path("configs"),
    planner_backend: PlannerBackend | None = None,
    now: datetime | None = None,
    state_review_snapshot_path: Path | None = None,
    execution_strategy_inputs: ExecutionStrategySelectorInput | dict[str, Any] | None = None,
) -> ContractPlanResult:
    objective = load_yaml_model(objective_path, ReleaseObjective)
    existing_contracts = _contracts_for_release(objective.release_id, contracts_dir)
    warnings: list[str] = []
    generated_contracts: list[GeneratedContract] = []

    budget_ledger_path = None
    planner_prompt_path = None
    config = load_project_config(project_id, config_dir, validate_repo=True) if project_id is not None else None
    if mode not in {"deterministic", "strong-model"}:
        raise ValueError(f"unsupported planning mode: {mode}")

    plan_dir = runs_dir / make_plan_id(objective.release_id, now)
    plan_dir.mkdir(parents=True, exist_ok=True)
    execution_strategy_selection: ExecutionStrategySelection | None = None
    execution_strategy_selection_path: Path | None = None
    supervisor_decision_path: Path | None = None
    one_shot_execution_input_path: Path | None = None
    selector_inputs: ExecutionStrategySelectorInput | None = None
    if execution_strategy_inputs is not None:
        selector_inputs = ExecutionStrategySelectorInput.model_validate(execution_strategy_inputs)
        if selector_inputs.release_id != objective.release_id:
            raise ValueError(
                "execution strategy selector release_id "
                f"{selector_inputs.release_id!r} did not match objective release_id {objective.release_id!r}"
            )
        execution_strategy_selection = select_execution_strategy(selector_inputs)
        execution_strategy_selection_path = plan_dir / "execution_strategy_selection.json"
        execution_strategy_selection_path.write_text(
            json.dumps(execution_strategy_selection.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        supervisor_decision = _build_execution_strategy_decision(
            decision_id=plan_dir.name,
            objective=objective,
            selector_inputs=selector_inputs,
            selection=execution_strategy_selection,
            plan_dir=plan_dir,
            state_review_snapshot_path=state_review_snapshot_path,
            now=now,
        )
        if supervisor_decision is not None:
            supervisor_decision_path = write_supervisor_decision_artifact(
                release_bundle_path=plan_dir,
                decision=supervisor_decision,
            )

        if execution_strategy_selection.selected_action in {
            SelectorExecutionStrategyAction.ONE_SHOT,
            SelectorExecutionStrategyAction.REPLAN,
            SelectorExecutionStrategyAction.STOP,
        }:
            warnings.append(
                "Execution strategy selection skipped contract generation: "
                f"{execution_strategy_selection.selected_action.value} ({execution_strategy_selection.reason.value})"
            )
            if execution_strategy_selection.selected_action == SelectorExecutionStrategyAction.ONE_SHOT:
                one_shot_execution_input_path = _write_one_shot_execution_input(
                    plan_dir=plan_dir,
                    objective=objective,
                    objective_path=objective_path,
                    project_id=project_id,
                    config_dir=config_dir if project_id is not None else None,
                    project_config=config,
                    selector_inputs=selector_inputs,
                    selection=execution_strategy_selection,
                    state_review_snapshot_path=state_review_snapshot_path,
                    now=now,
                )
            plan = ContractPlan(
                release_id=objective.release_id,
                planner=f"{mode}-gated",
                generated_contracts=[],
                warnings=warnings,
                budget_ledger_path=None,
                planner_prompt_path=None,
                state_review_snapshot_path=state_review_snapshot_path,
            )
            plan_path = plan_dir / "contract_plan.json"
            plan_path.write_text(
                json.dumps(plan.model_dump(mode="json"), indent=2) + "\n",
                encoding="utf-8",
            )
            return ContractPlanResult(
                release_id=objective.release_id,
                plan_path=plan_path,
                plan=plan,
                written_contract_paths=[],
                execution_strategy_selection=execution_strategy_selection,
                execution_strategy_selection_path=execution_strategy_selection_path,
                supervisor_decision_path=supervisor_decision_path,
                one_shot_execution_input_path=one_shot_execution_input_path,
            )

    if mode == "strong-model":
        if config is None:
            raise ValueError("strong-model planning requires --project")
        planner = config.model_roles.get("planner", config.executor)
        planner_prompt = _planner_prompt(objective, existing_contracts)
        budget_ledger_path = reserve_strong_model_call(
            runs_dir=runs_dir,
            release_id=objective.release_id,
            budget=config.budget,
            model=planner.model,
            reason="release planning",
            now=now,
        )
        if planner_backend is None:
            warnings.append(
                "Strong-model planning backend was not executed; budget was reserved and a planner prompt was written."
            )
    plan: ContractPlan | None = None
    if mode == "strong-model":
        planner_prompt_path = plan_dir / "planner_prompt.md"
        planner_prompt_path.write_text(planner_prompt, encoding="utf-8")
        if planner_backend is not None:
            plan_backend = _planner_backend_for_plan(planner_backend, plan_dir / "planner_backend")
            backend_output = plan_backend.generate(
                prompt=planner_prompt,
                objective=objective,
                existing_contracts=existing_contracts,
                model=planner.model,
            )
            backend_paths = _planner_backend_paths(backend_output)
            raw_output = backend_output.raw_output if isinstance(backend_output, PlannerBackendResult) else backend_output
            plan = parse_planner_output(
                raw_output,
                release_id=objective.release_id,
                planner=mode,
                project_config=config,
            )
            plan = plan.model_copy(
                update={
                    "budget_ledger_path": budget_ledger_path,
                    "planner_prompt_path": planner_prompt_path,
                    "state_review_snapshot_path": state_review_snapshot_path,
                    **backend_paths,
                }
            )

    if plan is None:
        if not existing_contracts:
            warnings.append(
                "No existing contracts found for release; generated output is a conservative draft."
            )
            generated_contracts.append(
                _draft_release_preparation_contract(
                    objective,
                    verification_profile=_verification_profile_for_draft(config, "release_preparation"),
                )
            )
        else:
            covered = {contract.task_id for contract in existing_contracts}
            warnings.append(
                f"Existing contracts found; {mode} planner validates decomposition but does not rewrite contracts."
            )
            for index, criterion in enumerate(objective.acceptance_criteria, start=1):
                generated_contracts.append(
                    GeneratedContract(
                        task_id=f"criterion-review-{index:04d}",
                        title=f"Validate criterion: {criterion[:60]}",
                        objective=f"Confirm release contracts cover acceptance criterion: {criterion}",
                        rationale=f"Current contracts for {objective.release_id}: {', '.join(sorted(covered))}",
                        suggested_contract=_criterion_review_contract(
                            objective,
                            index,
                            criterion,
                            covered,
                            verification_profile=_verification_profile_for_draft(config, "documentation"),
                        ),
                    )
                )

        plan = ContractPlan(
            release_id=objective.release_id,
            planner=mode,
            generated_contracts=generated_contracts,
            warnings=warnings,
            budget_ledger_path=budget_ledger_path,
            planner_prompt_path=planner_prompt_path,
            state_review_snapshot_path=state_review_snapshot_path,
        )

    if config is not None:
        validate_generated_contracts(plan, project_config=config)

    written_contract_paths: list[Path] = []
    if write_contracts_dir is not None:
        written_contract_paths = write_generated_contracts(
            plan,
            write_contracts_dir,
            project_config=config,
        )

    plan_path = plan_dir / "contract_plan.json"
    plan_path.write_text(json.dumps(plan.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return ContractPlanResult(
        release_id=objective.release_id,
        plan_path=plan_path,
        plan=plan,
        written_contract_paths=written_contract_paths,
        execution_strategy_selection=execution_strategy_selection,
        execution_strategy_selection_path=execution_strategy_selection_path,
        supervisor_decision_path=supervisor_decision_path,
        one_shot_execution_input_path=one_shot_execution_input_path,
    )


def _build_execution_strategy_decision(
    *,
    decision_id: str,
    objective: ReleaseObjective,
    selector_inputs: ExecutionStrategySelectorInput,
    selection: ExecutionStrategySelection,
    plan_dir: Path,
    state_review_snapshot_path: Path | None,
    now: datetime | None,
) -> ExecutionStrategyDecision | None:
    action_mapping: dict[SelectorExecutionStrategyAction, SupervisorExecutionStrategyAction] = {
        SelectorExecutionStrategyAction.ONE_SHOT: SupervisorExecutionStrategyAction.ONE_SHOT,
        SelectorExecutionStrategyAction.SEQUENTIAL_CONTRACTS: SupervisorExecutionStrategyAction.SEQUENTIAL_CONTRACTS,
        SelectorExecutionStrategyAction.PARALLEL_CONTRACTS: SupervisorExecutionStrategyAction.PARALLEL_CONTRACTS,
        SelectorExecutionStrategyAction.STACKED_BRANCHES: SupervisorExecutionStrategyAction.STACKED_BRANCHES,
        SelectorExecutionStrategyAction.PATCH_HANDOFF: SupervisorExecutionStrategyAction.PATCH_HANDOFF,
        SelectorExecutionStrategyAction.REPLAN: SupervisorExecutionStrategyAction.REPLAN,
    }
    if selection.selected_action not in action_mapping:
        return None

    selected_action = action_mapping[selection.selected_action]
    outcome_mapping: dict[SupervisorExecutionStrategyAction, ExecutionStrategyOutcome] = {
        SupervisorExecutionStrategyAction.ONE_SHOT: ExecutionStrategyOutcome.PROCEED_ONE_SHOT,
        SupervisorExecutionStrategyAction.SEQUENTIAL_CONTRACTS: ExecutionStrategyOutcome.PROCEED_SEQUENTIAL,
        SupervisorExecutionStrategyAction.PARALLEL_CONTRACTS: ExecutionStrategyOutcome.PROCEED_PARALLEL,
        SupervisorExecutionStrategyAction.STACKED_BRANCHES: ExecutionStrategyOutcome.PROCEED_STACKED,
        SupervisorExecutionStrategyAction.PATCH_HANDOFF: ExecutionStrategyOutcome.PROCEED_PATCH_HANDOFF,
        SupervisorExecutionStrategyAction.REPLAN: ExecutionStrategyOutcome.REPLAN,
    }
    outcome = outcome_mapping[selected_action]
    evidence_paths: list[Path] = []
    if state_review_snapshot_path is not None and state_review_snapshot_path.exists():
        evidence_paths.append(state_review_snapshot_path)
    if plan_dir.exists():
        selection_path = plan_dir / "execution_strategy_selection.json"
        if selection_path.exists():
            evidence_paths.append(selection_path.relative_to(plan_dir))

    risk_level = DecisionRiskLevel.MODERATE
    if selection.selected_action == SelectorExecutionStrategyAction.REPLAN:
        risk_level = DecisionRiskLevel.HIGH
    if selection.selected_action == SelectorExecutionStrategyAction.ONE_SHOT:
        risk_level = DecisionRiskLevel.MODERATE

    fallback_plan = "Replan the release into bounded contracts and rerun planning gates before execution."
    if selection.selected_action == SelectorExecutionStrategyAction.ONE_SHOT:
        fallback_plan = "Fallback to sequential contract decomposition if one-shot execution cannot stay cohesive."

    validators_to_rerun = ["objective_scope", "verification"]
    return ExecutionStrategyDecision.model_validate(
        {
            "decision_type": SupervisorDecisionType.EXECUTION_STRATEGY,
            "decision_id": decision_id,
            "release_id": objective.release_id,
            "decided_at": now or datetime.now(UTC),
            "decided_by": "execution_strategy_selector",
            "rationale": (
                f"Selected {selection.selected_action.value} because {selection.reason.value}. "
                f"Objective title={objective.title!r} task_ids={selector_inputs.task_ids!r}."
            ),
            "evidence_paths": [str(path) for path in evidence_paths],
            "risk_level": risk_level,
            "selected_action": selected_action,
            "outcome": outcome,
            "fallback_plan": fallback_plan,
            "validators_to_rerun": validators_to_rerun,
        }
    )


def _write_one_shot_execution_input(
    *,
    plan_dir: Path,
    objective: ReleaseObjective,
    objective_path: Path,
    project_id: str | None,
    config_dir: Path | None,
    project_config: ProjectConfig | None,
    selector_inputs: ExecutionStrategySelectorInput,
    selection: ExecutionStrategySelection,
    state_review_snapshot_path: Path | None,
    now: datetime | None,
) -> Path:
    evidence = [
        "git diff",
        "git status --short --branch",
        "pytest output",
        "contract plan JSON",
        "supervisor decision record",
    ]
    stop_conditions = [
        "Forbidden paths change detected.",
        "Generated artifacts change detected.",
        "Lockfile change detected.",
        "Migration change detected.",
        "Unsafe policy expansion detected.",
        "Missing required evidence.",
        "Verification failed.",
        "Finalization policy blocked.",
        "Objective scope expands beyond bounded one-shot intent.",
    ]
    scope = {
        "strategy": "one_shot",
        "task_ids": selector_inputs.task_ids,
        "objective_title": objective.title,
        "objective_summary": objective.objective,
        "acceptance_criteria": objective.acceptance_criteria,
        "non_goals": objective.non_goals,
        "state_review_snapshot_path": str(state_review_snapshot_path) if state_review_snapshot_path else None,
    }
    payload = OneShotExecutionInput(
        release_id=objective.release_id,
        created_at=now or datetime.now(UTC),
        created_by="plan_release_contracts",
        objective_path=objective_path,
        objective=objective,
        project_id=project_id,
        config_dir=config_dir,
        budget=project_config.budget.model_dump(mode="json") if project_config is not None else None,
        verification_profiles=(
            {name: profile.model_dump(mode="json") for name, profile in project_config.verification_profiles.items()}
            if project_config is not None
            else None
        ),
        scope=scope,
        evidence_requirements=evidence,
        stop_conditions=stop_conditions,
        selector_inputs=selector_inputs,
        selector_selection=selection,
    )
    target_path = plan_dir / "one_shot_execution_input.json"
    target_path.write_text(
        json.dumps(payload.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target_path


def _contracts_for_release(release_id: str, contracts_dir: Path) -> list[TaskContract]:
    contracts: list[TaskContract] = []
    for path in sorted(contracts_dir.glob("*.yaml")):
        contract = load_yaml_model(path, TaskContract)
        if contract.release_id == release_id:
            contracts.append(contract)
    return contracts


def _draft_release_preparation_contract(
    objective: ReleaseObjective,
    *,
    verification_profile: str,
) -> GeneratedContract:
    task_id = f"{objective.release_id}-0001".replace(".", "-")
    suggested = {
        "task_id": task_id,
        "release_id": objective.release_id,
        "title": f"Prepare contract set for {objective.release_id}",
        "task_type": "release_preparation",
        "budget_class": "L",
        "objective": "Create bounded implementation contracts for the approved release objective.",
        "allowed_files": ["contracts/**", f"repo_state/**/release_plan.yaml"],
        "forbidden_changes": ["Do not modify source code while planning contracts."],
        "required_evidence": ["git diff", "contract diff", "release plan diff"],
        "verification": {"profile": verification_profile},
        "stop_conditions": [
            "Generated contracts cannot be bounded to allowed files.",
            "Verification profiles cannot be matched to project config.",
        ],
    }
    return GeneratedContract(
        task_id=task_id,
        title=suggested["title"],
        objective=suggested["objective"],
        rationale="No contracts exist yet; start with a planning-only release-preparation task.",
        suggested_contract=TaskContract.model_validate(suggested),
    )


def _criterion_review_contract(
    objective: ReleaseObjective,
    index: int,
    criterion: str,
    covered: set[str],
    *,
    verification_profile: str,
) -> TaskContract:
    return TaskContract.model_validate(
        {
            "task_id": f"criterion-review-{index:04d}",
            "release_id": objective.release_id,
            "title": f"Validate criterion: {criterion[:60]}",
            "task_type": "documentation",
            "budget_class": "S",
            "objective": f"Confirm release contracts cover acceptance criterion: {criterion}",
            "allowed_files": ["contracts/**"],
            "forbidden_changes": [
                "Do not modify source code while validating release coverage.",
            ],
            "required_evidence": [
                "git diff",
                f"Coverage note for {criterion}",
            ],
            "verification": {"profile": verification_profile},
            "stop_conditions": [
                f"Acceptance criterion is not covered by current contracts: {', '.join(sorted(covered))}",
                "Coverage review requires source code changes.",
            ],
        }
    )


def parse_planner_output(
    raw_output: str | dict[str, Any] | ContractPlan,
    *,
    release_id: str,
    planner: str,
    project_config: ProjectConfig | None = None,
) -> ContractPlan:
    if isinstance(raw_output, str):
        raw_output = _extract_json_object(raw_output)
        try:
            raw_output = json.loads(raw_output)
        except json.JSONDecodeError as error:
            raise ValueError("planner output must be valid JSON") from error
    if isinstance(raw_output, dict):
        raw_output = _normalize_planner_contract_payloads(raw_output, release_id=release_id)
    supervisor = RuntimeSupervisor()
    normalization = supervisor.apply_planner_contract_normalization(
        source_evidence_paths=(),
        candidate_plan=raw_output,
    )
    if not normalization.applied or normalization.proposal is None:
        reason = (
            normalization.stop_evidence.reason
            if normalization.stop_evidence is not None
            else "Planner normalization failed ContractPlan/TaskContract validation."
        )
        raise PlannerNormalizationError(
            "planner output did not match the contract plan schema",
            stop_evidence=PlannerNormalizationStopEvidence(
                kind=RuntimeSupervisorApplierStopKind.BYPASSES_HARD_GATE,
                reason=reason,
            ),
        )
    plan = normalization.proposal.normalized_plan
    if plan.release_id != release_id:
        raise ValueError(
            f"planner output release_id {plan.release_id!r} did not match expected {release_id!r}"
        )
    plan = plan.model_copy(update={"planner": planner})
    plan = _normalize_contracts_for_admission(plan, project_config=project_config)
    try:
        validate_generated_contracts(plan, project_config=project_config)
    except ValueError as error:
        message = str(error)
        if "unsafe whole-repo allowed_files patterns" in message:
            raise PlannerNormalizationError(
                message,
                stop_evidence=PlannerNormalizationStopEvidence(
                    kind=RuntimeSupervisorApplierStopKind.BROADENS_ALLOWED_FILES,
                    reason=message,
                ),
            ) from error
        if "allowed_files count exceeds project budget" in message:
            raise PlannerNormalizationError(
                message,
                stop_evidence=PlannerNormalizationStopEvidence(
                    kind=RuntimeSupervisorApplierStopKind.EXCEEDS_TASK_BUDGET,
                    reason=message,
                ),
            ) from error
        raise
    return plan


def _normalize_planner_contract_payloads(raw_plan: dict[str, Any], *, release_id: str) -> dict[str, Any]:
    """Repair wrapper-level planner drift before strict ContractPlan validation."""
    normalized_plan = deepcopy(raw_plan)
    generated_contracts = normalized_plan.get("generated_contracts")
    if not isinstance(generated_contracts, list):
        return normalized_plan

    warnings = list(normalized_plan.get("warnings") or [])
    plan_release_id = str(normalized_plan.get("release_id") or release_id)
    normalized_generated_contracts: list[Any] = []
    for generated in generated_contracts:
        if not isinstance(generated, dict):
            normalized_generated_contracts.append(generated)
            continue
        suggested_contract = generated.get("suggested_contract")
        if not isinstance(suggested_contract, dict):
            normalized_generated_contracts.append(generated)
            continue

        contract_payload = deepcopy(suggested_contract)
        fallback_fields = {
            "task_id": generated.get("task_id"),
            "release_id": plan_release_id,
            "title": generated.get("title"),
            "objective": generated.get("objective"),
            "budget_class": generated.get("budget_class") or "M",
        }
        changed_fields: list[str] = []
        for field_name, fallback_value in fallback_fields.items():
            if field_name not in contract_payload and fallback_value:
                contract_payload[field_name] = fallback_value
                changed_fields.append(field_name)
        if "required_evidence" not in contract_payload:
            contract_payload["required_evidence"] = ["git diff", "changed-files list"]
            changed_fields.append("required_evidence")
        if isinstance(contract_payload.get("verification"), list):
            contract_payload["verification"] = {"commands": contract_payload["verification"]}
            changed_fields.append("verification")
        if "requirements" in contract_payload:
            contract_payload.pop("requirements")
            changed_fields.append("requirements")

        contract, alias_changes, refusal_reasons = normalize_task_contract_payload(contract_payload)
        if contract is None:
            normalized_generated_contracts.append(generated)
            if refusal_reasons:
                warnings.append(
                    "planner_contract_payload_normalization_refused="
                    + json.dumps(
                        {
                            "task_id": generated.get("task_id"),
                            "refusal_reasons": [str(reason) for reason in refusal_reasons],
                        },
                        sort_keys=True,
                    )
                )
            continue
        if not _has_quality_stop_condition(contract):
            updated_stop_conditions = [
                *contract.stop_conditions,
                "Stop if scope or verification cannot remain within the generated contract.",
            ]
            contract = contract.model_copy(update={"stop_conditions": updated_stop_conditions})
            changed_fields.append("stop_conditions")

        if changed_fields or alias_changes:
            generated = deepcopy(generated)
            generated["suggested_contract"] = contract.model_dump(mode="python")
            warnings.append(
                "planner_contract_payload_normalization="
                + json.dumps(
                    {
                        "task_id": generated.get("task_id"),
                        "changed_fields": [
                            *changed_fields,
                            *[field.path for field in alias_changes],
                        ],
                    },
                    sort_keys=True,
                )
            )
        normalized_generated_contracts.append(generated)

    normalized_plan["generated_contracts"] = normalized_generated_contracts
    normalized_plan["warnings"] = warnings
    return normalized_plan


def _normalize_contracts_for_admission(
    plan: ContractPlan,
    *,
    project_config: ProjectConfig | None = None,
) -> ContractPlan:
    normalized_generated: list[GeneratedContract] = []
    normalized_evidence: list[str] = []
    for generated in plan.generated_contracts:
        single_plan = plan.model_copy(update={"generated_contracts": [generated]})
        should_normalize = True
        try:
            validate_generated_contracts(single_plan, project_config=project_config)
        except ValueError as error:
            if "must require diff evidence" not in str(error):
                normalized_generated.append(generated)
                continue
        else:
            should_normalize = _contract_needs_runtime_normalization(
                generated.suggested_contract,
                project_config=project_config,
            )
        if not should_normalize:
            normalized_generated.append(generated)
            continue

        request = ContractNormalizationRequest(
            release_id=plan.release_id,
            task_id=generated.task_id,
            rationale="Repair planner-generated admission failure with deterministic normalization.",
            before_snapshot={"contract": generated.suggested_contract},
            artifact_paths={
                "planner_prompt_path": plan.planner_prompt_path,
                "planner_stdout_path": plan.planner_stdout_path,
                "planner_stderr_path": plan.planner_stderr_path,
                "planner_metadata_path": plan.planner_metadata_path,
            },
        )
        outcome = normalize_contract_request(request, project_config=project_config)
        if outcome.after_snapshot is None:
            raise PlannerNormalizationError(
                "planner-generated contract normalization was refused",
                stop_evidence=PlannerNormalizationStopEvidence(
                    kind=RuntimeSupervisorApplierStopKind.BYPASSES_HARD_GATE,
                    reason=f"Normalization refused for {generated.task_id}.",
                ),
            )
        normalized_contract = outcome.after_snapshot.contract
        if (
            normalized_contract.allowed_files != generated.suggested_contract.allowed_files
            or normalized_contract.forbidden_changes != generated.suggested_contract.forbidden_changes
            or normalized_contract.depends_on != generated.suggested_contract.depends_on
        ):
            raise PlannerNormalizationError(
                "planner-generated contract normalization changed guarded semantics",
                stop_evidence=PlannerNormalizationStopEvidence(
                    kind=RuntimeSupervisorApplierStopKind.BYPASSES_HARD_GATE,
                    reason=(
                        "Deterministic normalization attempted to modify allowed_files, forbidden_changes, "
                        f"or depends_on for {generated.task_id}."
                    ),
                ),
            )
        if not outcome.changed_fields:
            normalized_generated.append(generated)
            continue
        normalized_generated_contract = generated.model_copy(update={"suggested_contract": normalized_contract})
        rerun_plan = plan.model_copy(update={"generated_contracts": [normalized_generated_contract]})
        validate_generated_contracts(rerun_plan, project_config=project_config)
        normalized_generated.append(normalized_generated_contract)
        normalized_evidence.append(
            "planner_contract_normalization="
            + json.dumps(outcome.model_dump(mode="json"), sort_keys=True)
        )
    if not normalized_evidence:
        return plan
    return plan.model_copy(
        update={
            "generated_contracts": normalized_generated,
            "warnings": [*plan.warnings, *normalized_evidence],
        }
    )


def _contract_needs_runtime_normalization(
    contract: TaskContract,
    *,
    project_config: ProjectConfig | None,
) -> bool:
    if project_config is None:
        return False
    return any(command.startswith(".venv/bin/python") for command in contract.verification.commands)


def validate_generated_contracts(
    plan: ContractPlan,
    *,
    project_config: ProjectConfig | None = None,
) -> list[TaskContract]:
    validated_contracts: list[TaskContract] = []
    seen_task_ids: set[str] = set()
    for generated_contract in plan.generated_contracts:
        suggested_contract = TaskContract.model_validate(generated_contract.suggested_contract)
        if generated_contract.task_id != suggested_contract.task_id:
            raise ValueError(
                "generated contract task_id "
                f"{generated_contract.task_id!r} did not match suggested contract task_id "
                f"{suggested_contract.task_id!r}"
            )
        if suggested_contract.task_id in seen_task_ids:
            raise ValueError(f"generated contract task_id {suggested_contract.task_id!r} is duplicated")
        if suggested_contract.release_id != plan.release_id:
            raise ValueError(
                "generated contract release_id "
                f"{suggested_contract.release_id!r} did not match plan release_id {plan.release_id!r}"
            )
        broad_patterns = [pattern for pattern in suggested_contract.allowed_files if _is_whole_repo_pattern(pattern)]
        if broad_patterns:
            raise ValueError(
                "generated contract uses unsafe whole-repo allowed_files patterns: "
                + ", ".join(broad_patterns)
            )
        if not _has_required_evidence(suggested_contract, "diff"):
            raise ValueError(
                f"generated contract must require diff evidence: {suggested_contract.task_id}"
            )
        if not _has_quality_stop_condition(suggested_contract):
            raise ValueError(
                f"generated contract must include a scope or verification stop condition: {suggested_contract.task_id}"
            )
        if suggested_contract.task_type in {"benchmark", "scientific_validation"} and not suggested_contract.non_goals:
            raise ValueError(
                "generated scientific or benchmark contracts must include explicit non_goals: "
                f"{suggested_contract.task_id}"
            )
        if project_config is not None:
            if len(suggested_contract.allowed_files) > project_config.budget.max_changed_files_per_task:
                raise ValueError(
                    "generated contract allowed_files count exceeds project budget "
                    f"max_changed_files_per_task={project_config.budget.max_changed_files_per_task}: "
                    f"{suggested_contract.task_id}"
                )
            if suggested_contract.verification.profile is not None:
                profile = suggested_contract.verification.profile
                if profile not in project_config.verification_profiles:
                    raise ValueError(
                        f"generated contract references unknown verification profile {profile!r}: "
                        f"{suggested_contract.task_id}"
                    )
                if (
                    suggested_contract.task_type.value in project_config.verification_profiles
                    and profile != suggested_contract.task_type.value
                    and profile != "default"
                ):
                    raise ValueError(
                        "generated contract verification profile is inconsistent with task_type "
                        f"{suggested_contract.task_type.value!r}: {suggested_contract.task_id}"
                    )
        seen_task_ids.add(suggested_contract.task_id)
        validated_contracts.append(suggested_contract)
    return validated_contracts


def write_generated_contracts(
    plan: ContractPlan,
    contracts_dir: Path,
    *,
    project_config: ProjectConfig | None = None,
) -> list[Path]:
    validated_contracts = validate_generated_contracts(plan, project_config=project_config)
    contracts_dir.mkdir(parents=True, exist_ok=True)
    target_paths = [contracts_dir / f"{contract.task_id}.yaml" for contract in validated_contracts]
    existing_paths = [path for path in target_paths if path.exists()]
    if existing_paths:
        raise FileExistsError(
            "refusing to overwrite existing contract files: "
            + ", ".join(str(path) for path in existing_paths)
        )

    written_paths: list[Path] = []
    for contract, path in zip(validated_contracts, target_paths):
        written_paths.append(write_yaml_model(path, contract))
    return written_paths


def _planner_prompt(objective: ReleaseObjective, contracts: list[TaskContract]) -> str:
    contract_summaries = "\n".join(
        f"- {contract.task_id}: {contract.title}; allowed_files={contract.allowed_files}"
        for contract in contracts
    )
    if not contract_summaries:
        contract_summaries = "- No existing contracts."
    return "\n".join(
        [
            "# Strong Release Planning Prompt",
            "",
            "Produce bounded task contracts for the release objective. Do not emit broad tasks.",
            "Every proposed contract must include allowed files, forbidden changes, verification, and stop conditions.",
            "Return only one JSON object matching the ContractPlan schema. Do not include Markdown prose.",
            "",
            "Required JSON shape:",
            '{"release_id": "...", "planner": "strong-model", "generated_contracts": [{"task_id": "...", "title": "...", "objective": "...", "rationale": "...", "suggested_contract": {...}}], "warnings": []}',
            "",
            f"## Release: {objective.release_id}",
            "",
            objective.objective,
            "",
            "## Acceptance Criteria",
            "",
            *[f"- {criterion}" for criterion in objective.acceptance_criteria],
            "",
            "## Existing Contracts",
            "",
            contract_summaries,
            "",
        ]
    )


def _extract_json_object(raw_output: str) -> str:
    stripped = raw_output.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    if stripped.startswith("{"):
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return stripped[start : end + 1]
    return stripped


def _is_whole_repo_pattern(pattern: str) -> bool:
    normalized = pattern.strip().rstrip("/")
    return normalized in {"*", "**", "**/*", "./**", "./**/*"}


def _has_required_evidence(contract: TaskContract, needle: str) -> bool:
    needle_lower = needle.lower()
    return any(needle_lower in item.lower() for item in contract.required_evidence)


def _has_quality_stop_condition(contract: TaskContract) -> bool:
    terms = ("scope", "verification", "fail", "cannot", "allowed")
    return any(any(term in condition.lower() for term in terms) for condition in contract.stop_conditions)


def _planner_backend_paths(raw_output: object) -> dict[str, Path]:
    if not isinstance(raw_output, PlannerBackendResult):
        return {}
    return {
        "planner_stdout_path": raw_output.stdout_path,
        "planner_stderr_path": raw_output.stderr_path,
        "planner_metadata_path": raw_output.metadata_path,
    }


def _planner_backend_for_plan(planner_backend: PlannerBackend, output_dir: Path) -> PlannerBackend:
    with_output_dir = getattr(planner_backend, "with_output_dir", None)
    if callable(with_output_dir):
        return with_output_dir(output_dir)
    return planner_backend


def _verification_profile_for_draft(config: ProjectConfig | None, preferred: str) -> str:
    if config is None:
        return preferred
    if preferred in config.verification_profiles:
        return preferred
    if "default" in config.verification_profiles:
        return "default"
    return next(iter(config.verification_profiles))
