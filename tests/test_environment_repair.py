from __future__ import annotations

from agentic_devloop.environment_repair import (
    classify_verification_environment_repair_category,
    decide_verification_environment_repair,
)
from agentic_devloop.models import (
    VerificationEnvironmentRepairAction,
    VerificationEnvironmentRepairActionKind,
    VerificationEnvironmentRepairCategory,
    VerificationEnvironmentRepairInput,
    VerificationEnvironmentRepairPolicy,
    VerificationEnvironmentRepairRefusal,
    VerificationEnvironmentRepairRefusalReason,
)


def _policy(*, allowed_actions: list[VerificationEnvironmentRepairActionKind]) -> VerificationEnvironmentRepairPolicy:
    return VerificationEnvironmentRepairPolicy(
        allowed_actions=allowed_actions,
        allowed_files_snapshot=(
            "src/agentic_devloop/environment_repair.py",
            "src/agentic_devloop/models.py",
            "tests/test_environment_repair.py",
        ),
        editable_install_command="PYTHONPATH=src /runtime/python -m pip install -e .",
        pythonpath_prefix="PYTHONPATH=src",
    )


def _input(*, command: str, stderr_excerpt: str, stdout_excerpt: str = "") -> VerificationEnvironmentRepairInput:
    return VerificationEnvironmentRepairInput(
        command=command,
        exit_code=1,
        stderr_excerpt=stderr_excerpt,
        stdout_excerpt=stdout_excerpt,
        allowed_files_snapshot=[
            "src/agentic_devloop/environment_repair.py",
            "src/agentic_devloop/models.py",
            "tests/test_environment_repair.py",
        ],
    )


def test_classify_stale_editable_install() -> None:
    category = classify_verification_environment_repair_category(
        _input(
            command="PYTHONPATH=src /runtime/python -m pytest tests/test_environment_repair.py",
            stderr_excerpt="No module named agentic_devloop after editable install metadata drift; editable install looks stale",
        )
    )

    assert category == VerificationEnvironmentRepairCategory.STALE_EDITABLE_INSTALL


def test_classify_command_not_found() -> None:
    category = classify_verification_environment_repair_category(
        _input(
            command="pytest tests/test_environment_repair.py",
            stderr_excerpt="pytest: command not found",
        )
    )

    assert category == VerificationEnvironmentRepairCategory.COMMAND_NOT_FOUND


def test_classify_missing_pythonpath() -> None:
    category = classify_verification_environment_repair_category(
        _input(
            command="python -m pytest tests/test_environment_repair.py",
            stderr_excerpt="No module named agentic_devloop; set PYTHONPATH correctly",
        )
    )

    assert category == VerificationEnvironmentRepairCategory.MISSING_PYTHONPATH


def test_classify_runtime_dependency_drift() -> None:
    category = classify_verification_environment_repair_category(
        _input(
            command="python -m pytest tests/test_environment_repair.py",
            stderr_excerpt="ModuleNotFoundError: No module named pytest",
        )
    )

    assert category == VerificationEnvironmentRepairCategory.RUNTIME_DEPENDENCY_DRIFT


def test_unknown_category_refuses() -> None:
    decision = decide_verification_environment_repair(
        repair_input=_input(
            command="python -m pytest tests/test_environment_repair.py",
            stderr_excerpt="segmentation fault",
        ),
        policy=_policy(
            allowed_actions=[
                VerificationEnvironmentRepairActionKind.REFRESH_EDITABLE_INSTALL,
                VerificationEnvironmentRepairActionKind.SET_PYTHONPATH_PREFIX,
                VerificationEnvironmentRepairActionKind.CAPTURE_ENVIRONMENT,
            ]
        ),
    )

    assert isinstance(decision, VerificationEnvironmentRepairRefusal)
    assert decision.reason == VerificationEnvironmentRepairRefusalReason.UNCLASSIFIED_FAILURE


def test_policy_can_refuse_disallowed_action() -> None:
    decision = decide_verification_environment_repair(
        repair_input=_input(
            command="python -m pytest tests/test_environment_repair.py",
            stderr_excerpt="No module named agentic_devloop; set PYTHONPATH correctly",
        ),
        policy=_policy(allowed_actions=[VerificationEnvironmentRepairActionKind.CAPTURE_ENVIRONMENT]),
    )

    assert isinstance(decision, VerificationEnvironmentRepairRefusal)
    assert decision.reason == VerificationEnvironmentRepairRefusalReason.ACTION_NOT_ALLOWED_BY_POLICY


def test_allowed_files_snapshot_mismatch_refuses() -> None:
    repair_input = _input(
        command="python -m pytest tests/test_environment_repair.py",
        stderr_excerpt="pytest: command not found",
    )
    policy = _policy(allowed_actions=[VerificationEnvironmentRepairActionKind.CAPTURE_ENVIRONMENT])
    policy = policy.model_copy(update={"allowed_files_snapshot": ("src/agentic_devloop/models.py",)})

    decision = decide_verification_environment_repair(repair_input=repair_input, policy=policy)

    assert isinstance(decision, VerificationEnvironmentRepairRefusal)
    assert decision.reason == VerificationEnvironmentRepairRefusalReason.ALLOWED_FILES_MISMATCH


def test_stale_editable_install_selects_repair_action() -> None:
    decision = decide_verification_environment_repair(
        repair_input=_input(
            command="PYTHONPATH=src /runtime/python -m pytest tests/test_environment_repair.py",
            stderr_excerpt="No module named agentic_devloop and editable install metadata is stale",
        ),
        policy=_policy(
            allowed_actions=[
                VerificationEnvironmentRepairActionKind.REFRESH_EDITABLE_INSTALL,
            ]
        ),
    )

    assert isinstance(decision, VerificationEnvironmentRepairAction)
    assert decision.action == VerificationEnvironmentRepairActionKind.REFRESH_EDITABLE_INSTALL
