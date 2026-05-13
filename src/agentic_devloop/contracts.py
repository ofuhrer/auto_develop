from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
from typing import Any

from agentic_devloop.config import discover_safe_verification_runtime
from agentic_devloop.models import (
    ContractNormalizationChangedField,
    ContractNormalizationDecision,
    ContractNormalizationOutcome,
    ContractNormalizationRefusalReason,
    ContractNormalizationRequest,
    ProjectConfig,
    TaskContract,
)
from agentic_devloop.verification import rewrite_worktree_local_verification_command


_REQUIRED_EVIDENCE_ITEMS = ("git diff", "changed-files list")
_TASK_KEY_ALIASES = {
    "allowedFiles": "allowed_files",
    "forbiddenChanges": "forbidden_changes",
    "requiredEvidence": "required_evidence",
    "stopConditions": "stop_conditions",
    "dependsOn": "depends_on",
}


def normalize_contract_request(
    request: ContractNormalizationRequest,
    *,
    project_config: ProjectConfig | None = None,
) -> ContractNormalizationOutcome:
    before_contract = request.before_snapshot.contract
    changed_fields: list[ContractNormalizationChangedField] = []
    updated_contract = before_contract.model_copy(deep=True)

    required_evidence = list(updated_contract.required_evidence)
    for required in _REQUIRED_EVIDENCE_ITEMS:
        if required not in required_evidence:
            required_evidence.append(required)
    if required_evidence != updated_contract.required_evidence:
        changed_fields.append(
            ContractNormalizationChangedField(
                path="required_evidence",
                before=updated_contract.required_evidence,
                after=required_evidence,
            )
        )
        updated_contract = updated_contract.model_copy(update={"required_evidence": required_evidence})

    safe_runtime = discover_safe_verification_runtime(project_config)
    updated_commands = list(updated_contract.verification.commands)
    command_changed = False
    for index, command in enumerate(updated_commands):
        rewritten = rewrite_worktree_local_verification_command(command, safe_runtime=safe_runtime)
        if rewritten != command:
            command_changed = True
            changed_fields.append(
                ContractNormalizationChangedField(
                    path=f"verification.commands[{index}]",
                    before=command,
                    after=rewritten,
                )
            )
            updated_commands[index] = rewritten
    if command_changed:
        updated_contract = updated_contract.model_copy(
            update={"verification": updated_contract.verification.model_copy(update={"commands": updated_commands})}
        )

    return ContractNormalizationOutcome(
        release_id=request.release_id,
        task_id=request.task_id,
        decision=ContractNormalizationDecision.NORMALIZED,
        rationale="Applied deterministic repair-only normalization for admission-safe planner drift.",
        before_snapshot=request.before_snapshot,
        after_snapshot={"contract": updated_contract},
        changed_fields=changed_fields,
        artifact_paths=request.artifact_paths,
    )


def normalize_task_contract_payload(
    payload: Mapping[str, Any],
) -> tuple[TaskContract | None, list[ContractNormalizationChangedField], list[ContractNormalizationRefusalReason]]:
    normalized = deepcopy(dict(payload))
    changed_fields: list[ContractNormalizationChangedField] = []
    refusal_reasons: list[ContractNormalizationRefusalReason] = []

    for alias, canonical in _TASK_KEY_ALIASES.items():
        if alias not in normalized:
            continue
        if canonical in normalized:
            refusal_reasons.append(ContractNormalizationRefusalReason.AMBIGUOUS_CONTRACT_SEMANTICS)
            return None, changed_fields, _dedupe_refusal_reasons(refusal_reasons)
        changed_fields.append(
            ContractNormalizationChangedField(path=canonical, before=None, after=normalized[alias])
        )
        normalized[canonical] = normalized.pop(alias)

    verification = normalized.get("verification")
    verification_commands = normalized.pop("verificationCommands", None)
    if verification_commands is not None:
        if verification is not None:
            refusal_reasons.append(ContractNormalizationRefusalReason.AMBIGUOUS_CONTRACT_SEMANTICS)
            return None, changed_fields, _dedupe_refusal_reasons(refusal_reasons)
        changed_fields.append(
            ContractNormalizationChangedField(
                path="verification.commands",
                before=None,
                after=verification_commands,
            )
        )
        normalized["verification"] = {"commands": verification_commands}

    if isinstance(normalized.get("verification"), str):
        changed_fields.append(
            ContractNormalizationChangedField(
                path="verification",
                before=normalized["verification"],
                after={"commands": [normalized["verification"]]},
            )
        )
        normalized["verification"] = {"commands": [normalized["verification"]]}

    try:
        return TaskContract.model_validate(normalized), changed_fields, []
    except Exception:
        refusal_reasons.append(ContractNormalizationRefusalReason.UNSAFE_NORMALIZATION)
        return None, changed_fields, _dedupe_refusal_reasons(refusal_reasons)


def normalize_planner_contract_plan_payload(
    payload: Mapping[str, Any],
    *,
    release_id: str,
) -> dict[str, Any]:
    """Repair wrapper-level planner drift before strict ContractPlan validation."""
    normalized_plan = deepcopy(dict(payload))
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
        changed_fields: list[str] = []
        generated_depends_on = generated.get("depends_on")
        if "depends_on" not in contract_payload and isinstance(generated_depends_on, list):
            contract_payload["depends_on"] = generated_depends_on
            changed_fields.append("depends_on")
        fallback_fields = {
            "task_id": generated.get("task_id"),
            "release_id": plan_release_id,
            "title": generated.get("title"),
            "objective": generated.get("objective"),
            "budget_class": generated.get("budget_class") or "M",
        }
        for field_name, fallback_value in fallback_fields.items():
            if field_name not in contract_payload and fallback_value:
                contract_payload[field_name] = fallback_value
                changed_fields.append(field_name)
        if "required_evidence" not in contract_payload:
            contract_payload["required_evidence"] = list(_REQUIRED_EVIDENCE_ITEMS)
            changed_fields.append("required_evidence")
        if contract_payload.get("task_type") == "docs_and_tests":
            contract_payload["task_type"] = "release_preparation"
            changed_fields.append("task_type")
        if isinstance(contract_payload.get("verification"), list):
            contract_payload["verification"] = {"commands": contract_payload["verification"]}
            changed_fields.append("verification")
        implementation_requirement_sources = {
            "implementation_requirements": contract_payload.pop("implementation_requirements", None),
            "implementation_notes": contract_payload.pop("implementation_notes", None),
        }
        requirement_lines = [
            str(item).strip()
            for source_value in implementation_requirement_sources.values()
            if isinstance(source_value, list)
            for item in source_value
            if str(item).strip()
        ]
        if requirement_lines:
            repaired_source_fields = [
                field
                for field, source_value in implementation_requirement_sources.items()
                if isinstance(source_value, list) and source_value
            ]
            base_objective = str(contract_payload.get("objective") or generated.get("objective") or "").strip()
            contract_payload["objective"] = "\n".join(
                [
                    base_objective,
                    "",
                    "Implementation requirements:",
                    *[f"- {line}" for line in requirement_lines],
                ]
            ).strip()
            changed_fields.extend(repaired_source_fields)
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
            generated.pop("depends_on", None)
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


def _has_quality_stop_condition(contract: TaskContract) -> bool:
    terms = ("scope", "verification", "fail", "cannot", "allowed")
    return any(any(term in condition.lower() for term in terms) for condition in contract.stop_conditions)


def _dedupe_refusal_reasons(
    reasons: list[ContractNormalizationRefusalReason],
) -> list[ContractNormalizationRefusalReason]:
    deduped: list[ContractNormalizationRefusalReason] = []
    seen: set[ContractNormalizationRefusalReason] = set()
    for reason in reasons:
        if reason in seen:
            continue
        seen.add(reason)
        deduped.append(reason)
    return deduped
