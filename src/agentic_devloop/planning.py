from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agentic_devloop.budget import reserve_strong_model_call
from agentic_devloop.config import load_project_config
from agentic_devloop.models import ContractPlan, GeneratedContract, ReleaseObjective, TaskContract
from agentic_devloop.yaml_io import load_yaml_model


@dataclass(frozen=True)
class ContractPlanResult:
    release_id: str
    plan_path: Path
    plan: ContractPlan


def make_plan_id(release_id: str, now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{release_id}_plan"


def plan_release_contracts(
    *,
    objective_path: Path,
    contracts_dir: Path = Path("contracts"),
    runs_dir: Path = Path("runs"),
    mode: str = "deterministic",
    project_id: str | None = None,
    config_dir: Path = Path("configs"),
    now: datetime | None = None,
) -> ContractPlanResult:
    objective = load_yaml_model(objective_path, ReleaseObjective)
    existing_contracts = _contracts_for_release(objective.release_id, contracts_dir)
    warnings: list[str] = []
    generated_contracts: list[GeneratedContract] = []

    budget_ledger_path = None
    planner_prompt_path = None
    if mode not in {"deterministic", "strong-model"}:
        raise ValueError(f"unsupported planning mode: {mode}")
    if mode == "strong-model":
        if project_id is None:
            raise ValueError("strong-model planning requires --project")
        config = load_project_config(project_id, config_dir, validate_repo=True)
        planner = config.model_roles.get("planner", config.executor)
        budget_ledger_path = reserve_strong_model_call(
            runs_dir=runs_dir,
            release_id=objective.release_id,
            budget=config.budget,
            model=planner.model,
            reason="release planning",
            now=now,
        )
        warnings.append(
            "Strong-model planning backend is not implemented; budget was reserved and a planner prompt was written."
        )

    if not existing_contracts:
        warnings.append(
            "No existing contracts found for release; generated output is a conservative draft."
        )
        generated_contracts.append(_draft_release_preparation_contract(objective))
    else:
        covered = {contract.task_id for contract in existing_contracts}
        warnings.append(f"Existing contracts found; {mode} planner validates decomposition but does not rewrite contracts.")
        for index, criterion in enumerate(objective.acceptance_criteria, start=1):
            generated_contracts.append(
                GeneratedContract(
                    task_id=f"criterion-review-{index:04d}",
                    title=f"Validate criterion: {criterion[:60]}",
                    objective=f"Confirm release contracts cover acceptance criterion: {criterion}",
                    rationale=f"Current contracts for {objective.release_id}: {', '.join(sorted(covered))}",
                    suggested_contract={},
                )
            )

    plan = ContractPlan(
        release_id=objective.release_id,
        planner=mode,
        generated_contracts=generated_contracts,
        warnings=warnings,
        budget_ledger_path=budget_ledger_path,
    )
    plan_dir = runs_dir / make_plan_id(objective.release_id, now)
    plan_dir.mkdir(parents=True, exist_ok=True)
    if mode == "strong-model":
        planner_prompt_path = plan_dir / "planner_prompt.md"
        planner_prompt_path.write_text(_planner_prompt(objective, existing_contracts), encoding="utf-8")
        plan = plan.model_copy(update={"planner_prompt_path": planner_prompt_path})
    plan_path = plan_dir / "contract_plan.json"
    plan_path.write_text(json.dumps(plan.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return ContractPlanResult(release_id=objective.release_id, plan_path=plan_path, plan=plan)


def _contracts_for_release(release_id: str, contracts_dir: Path) -> list[TaskContract]:
    contracts: list[TaskContract] = []
    for path in sorted(contracts_dir.glob("*.yaml")):
        contract = load_yaml_model(path, TaskContract)
        if contract.release_id == release_id:
            contracts.append(contract)
    return contracts


def _draft_release_preparation_contract(objective: ReleaseObjective) -> GeneratedContract:
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
        "required_evidence": ["contract diff", "release plan diff"],
        "verification": {"profile": "documentation"},
        "stop_conditions": ["Generated contracts cannot be bounded to allowed files."],
    }
    return GeneratedContract(
        task_id=task_id,
        title=suggested["title"],
        objective=suggested["objective"],
        rationale="No contracts exist yet; start with a planning-only release-preparation task.",
        suggested_contract=suggested,
    )


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
