from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from agentic_devloop.budget import reserve_strong_model_call
from agentic_devloop.config import load_project_config
from agentic_devloop.models import ContractPlan, GeneratedContract, ProjectConfig, ReleaseObjective, TaskContract
from agentic_devloop.planner_backend import PlannerBackendResult
from agentic_devloop.security import validate_identifier
from agentic_devloop.yaml_io import load_yaml_model, write_yaml_model


@dataclass(frozen=True)
class ContractPlanResult:
    release_id: str
    plan_path: Path
    plan: ContractPlan
    written_contract_paths: list[Path] = field(default_factory=list)


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
    validate_identifier(release_id, kind="release_id")
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
) -> ContractPlanResult:
    objective = load_yaml_model(objective_path, ReleaseObjective)
    validate_identifier(objective.release_id, kind="release_id")
    existing_contracts = _contracts_for_release(objective.release_id, contracts_dir)
    warnings: list[str] = []
    generated_contracts: list[GeneratedContract] = []

    budget_ledger_path = None
    planner_prompt_path = None
    config = load_project_config(project_id, config_dir, validate_repo=True) if project_id is not None else None
    if mode not in {"deterministic", "strong-model"}:
        raise ValueError(f"unsupported planning mode: {mode}")
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

    plan_dir = runs_dir / make_plan_id(objective.release_id, now)
    plan_dir.mkdir(parents=True, exist_ok=True)
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
            plan = parse_planner_output(raw_output, release_id=objective.release_id, planner=mode)
            plan = plan.model_copy(
                update={
                    "budget_ledger_path": budget_ledger_path,
                    "planner_prompt_path": planner_prompt_path,
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
    )


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
        "allowed_files": ["contracts/**", "repo_state/**/release_plan.yaml"],
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
) -> ContractPlan:
    if isinstance(raw_output, str):
        raw_output = _extract_json_object(raw_output)
        try:
            raw_output = json.loads(raw_output)
        except json.JSONDecodeError as error:
            raise ValueError("planner output must be valid JSON") from error
    try:
        plan = ContractPlan.model_validate(raw_output)
    except ValidationError as error:
        raise ValueError("planner output did not match the contract plan schema") from error
    if plan.release_id != release_id:
        raise ValueError(
            f"planner output release_id {plan.release_id!r} did not match expected {release_id!r}"
        )
    plan = plan.model_copy(update={"planner": planner})
    validate_generated_contracts(plan)
    return plan


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
        if suggested_contract.verification.commands:
            raise ValueError(
                "generated contract must reference an approved verification profile instead of inline commands: "
                f"{suggested_contract.task_id}"
            )
        if suggested_contract.verification.profile is None:
            raise ValueError(
                f"generated contract must reference an approved verification profile: {suggested_contract.task_id}"
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
        if suggested_contract.task_type in {"benchmark", "scientific_validation", "validation"} and not suggested_contract.non_goals:
            raise ValueError(
                "generated validation or benchmark contracts must include explicit non_goals: "
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
