from __future__ import annotations

from agentic_devloop.models import (
    VerificationEnvironmentRepairAction,
    VerificationEnvironmentRepairActionKind,
    VerificationEnvironmentRepairCategory,
    VerificationEnvironmentRepairInput,
    VerificationEnvironmentRepairPolicy,
    VerificationEnvironmentRepairRefusal,
    VerificationEnvironmentRepairRefusalReason,
)


def _normalized_evidence_text(repair_input: VerificationEnvironmentRepairInput) -> str:
    return "\n".join(
        [
            repair_input.command,
            repair_input.stderr_excerpt,
            repair_input.stdout_excerpt,
        ]
    ).lower()


def classify_verification_environment_repair_category(
    repair_input: VerificationEnvironmentRepairInput,
) -> VerificationEnvironmentRepairCategory:
    evidence = _normalized_evidence_text(repair_input)
    command = repair_input.command.lower()

    if "editable" in evidence and "no module named" in evidence:
        return VerificationEnvironmentRepairCategory.STALE_EDITABLE_INSTALL

    if any(token in evidence for token in ("command not found", "not recognized as an internal or external command")):
        return VerificationEnvironmentRepairCategory.COMMAND_NOT_FOUND

    missing_module = "no module named" in evidence or "modulenotfounderror" in evidence
    if missing_module:
        if "pythonpath" in evidence:
            return VerificationEnvironmentRepairCategory.MISSING_PYTHONPATH
        # Deterministic fallback for local-package import context failures.
        if any(
            token in evidence
            for token in (
                "no module named agentic_devloop",
                "no module named 'agentic_devloop'",
                "no module named \"agentic_devloop\"",
            )
        ):
            return VerificationEnvironmentRepairCategory.MISSING_PYTHONPATH
        if "python" in command and "-c" in command and "pythonpath" not in command:
            return VerificationEnvironmentRepairCategory.MISSING_PYTHONPATH

    if missing_module or "module not found" in evidence:
        if "pytest" in command or "python -m pytest" in command:
            return VerificationEnvironmentRepairCategory.RUNTIME_DEPENDENCY_DRIFT

    return VerificationEnvironmentRepairCategory.UNKNOWN


def decide_verification_environment_repair(
    *,
    repair_input: VerificationEnvironmentRepairInput,
    policy: VerificationEnvironmentRepairPolicy,
) -> VerificationEnvironmentRepairAction | VerificationEnvironmentRepairRefusal:
    category = classify_verification_environment_repair_category(repair_input)

    if category == VerificationEnvironmentRepairCategory.UNKNOWN:
        return VerificationEnvironmentRepairRefusal(
            category=category,
            reason=VerificationEnvironmentRepairRefusalReason.UNCLASSIFIED_FAILURE,
            rationale="Failure does not match deterministic verification-environment repair categories.",
        )

    action_by_category = {
        VerificationEnvironmentRepairCategory.STALE_EDITABLE_INSTALL: VerificationEnvironmentRepairActionKind.REFRESH_EDITABLE_INSTALL,
        VerificationEnvironmentRepairCategory.COMMAND_NOT_FOUND: VerificationEnvironmentRepairActionKind.CAPTURE_ENVIRONMENT,
        VerificationEnvironmentRepairCategory.MISSING_PYTHONPATH: VerificationEnvironmentRepairActionKind.SET_PYTHONPATH_PREFIX,
        VerificationEnvironmentRepairCategory.RUNTIME_DEPENDENCY_DRIFT: VerificationEnvironmentRepairActionKind.CAPTURE_ENVIRONMENT,
    }
    selected_action = action_by_category[category]

    if selected_action not in policy.allowed_actions:
        return VerificationEnvironmentRepairRefusal(
            category=category,
            reason=VerificationEnvironmentRepairRefusalReason.ACTION_NOT_ALLOWED_BY_POLICY,
            rationale="Policy does not allow the classified verification-environment repair action.",
        )

    if category == VerificationEnvironmentRepairCategory.MISSING_PYTHONPATH and not policy.pythonpath_prefix:
        return VerificationEnvironmentRepairRefusal(
            category=category,
            reason=VerificationEnvironmentRepairRefusalReason.MISSING_POLICY_CONFIGURATION,
            rationale="Policy requires pythonpath_prefix for missing-PYTHONPATH repair.",
        )

    if policy.allowed_files_snapshot != tuple(repair_input.allowed_files_snapshot):
        return VerificationEnvironmentRepairRefusal(
            category=category,
            reason=VerificationEnvironmentRepairRefusalReason.ALLOWED_FILES_MISMATCH,
            rationale="Repair input allowed_files snapshot does not match policy snapshot.",
        )

    if category == VerificationEnvironmentRepairCategory.STALE_EDITABLE_INSTALL and not policy.editable_install_command:
        return VerificationEnvironmentRepairRefusal(
            category=category,
            reason=VerificationEnvironmentRepairRefusalReason.MISSING_POLICY_CONFIGURATION,
            rationale="Policy requires editable_install_command for stale editable install repair.",
        )

    return VerificationEnvironmentRepairAction(
        category=category,
        action=selected_action,
        rationale="Deterministic verification-environment repair action selected.",
    )
