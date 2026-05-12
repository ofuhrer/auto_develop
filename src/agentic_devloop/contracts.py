from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
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
