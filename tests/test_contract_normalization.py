from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agentic_devloop.config import discover_safe_verification_runtime
from agentic_devloop.contracts import (
    is_safe_worktree_python_verification_command,
    normalize_contract_request,
    normalize_task_contract_payload,
)
from agentic_devloop.models import (
    ContractNormalizationDecision,
    ContractNormalizationOutcome,
    ContractNormalizationRefusalReason,
    ContractNormalizationRequest,
    ProjectConfig,
    TaskContract,
)


def _task_contract(task_id: str = "demo-0001") -> dict[str, object]:
    return TaskContract(
        task_id=task_id,
        release_id="demo-release",
        title="Demo contract",
        budget_class="S",
        objective="Implement a small bounded change.",
        allowed_files=["src/demo.py"],
        required_evidence=["git diff", "changed-files list"],
        verification={"commands": ["pytest -q"]},
        stop_conditions=["Stop on unsafe change scope."],
    ).model_dump(mode="python")


def test_contract_normalization_models_validate_required_fields_and_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="rationale"):
        ContractNormalizationRequest.model_validate(
            {
                "release_id": "demo-release",
                "task_id": "demo-0001",
                "before_snapshot": {"contract": _task_contract()},
            }
        )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ContractNormalizationRequest.model_validate(
            {
                "release_id": "demo-release",
                "task_id": "demo-0001",
                "rationale": "Planner output requires bounded repair.",
                "before_snapshot": {"contract": _task_contract()},
                "unexpected": True,
            }
        )

    with pytest.raises(ValidationError, match="decision"):
        ContractNormalizationOutcome.model_validate(
            {
                "release_id": "demo-release",
                "task_id": "demo-0001",
                "rationale": "Missing decision.",
                "before_snapshot": {"contract": _task_contract()},
            }
        )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ContractNormalizationOutcome.model_validate(
            {
                "release_id": "demo-release",
                "task_id": "demo-0001",
                "decision": "normalized",
                "rationale": "Bounded normalization succeeded.",
                "before_snapshot": {"contract": _task_contract()},
                "unknown_field": "nope",
            }
        )


def test_contract_normalization_outcome_serializes_normalized_and_refused() -> None:
    normalized = ContractNormalizationOutcome.model_validate(
        {
            "release_id": "demo-release",
            "task_id": "demo-0001",
            "decision": ContractNormalizationDecision.NORMALIZED,
            "rationale": "Added missing evidence and updated verification command runtime.",
            "before_snapshot": {"contract": _task_contract()},
            "after_snapshot": {"contract": _task_contract()},
            "changed_fields": [
                {
                    "path": "required_evidence",
                    "before": ["git diff"],
                    "after": ["git diff", "changed-files list"],
                },
                {
                    "path": "verification.commands[0]",
                    "before": ".venv/bin/python -m pytest",
                    "after": "/shared/.venv/bin/python -m pytest",
                },
            ],
            "artifact_paths": {
                "planner_stdout_path": "runs/demo/planner_stdout.log",
                "normalization_log_path": "runs/demo/normalization.log",
            },
        }
    )
    refused = ContractNormalizationOutcome.model_validate(
        {
            "release_id": "demo-release",
            "task_id": "demo-0002",
            "decision": ContractNormalizationDecision.REFUSED,
            "rationale": "Suggested rewrite exceeded bounded normalization policy.",
            "before_snapshot": {"contract": _task_contract("demo-0002")},
            "refusal_reasons": [
                ContractNormalizationRefusalReason.OUT_OF_SCOPE_FILE_CHANGES,
                ContractNormalizationRefusalReason.UNSAFE_NORMALIZATION,
            ],
            "artifact_paths": {
                "planner_stderr_path": "runs/demo/planner_stderr.log",
                "normalization_log_path": "runs/demo/normalization.log",
            },
        }
    )

    normalized_dump = normalized.model_dump(mode="json")
    refused_dump = refused.model_dump(mode="json")

    assert normalized_dump["decision"] == "normalized"
    assert normalized_dump["before_snapshot"]["contract"]["task_id"] == "demo-0001"
    assert normalized_dump["after_snapshot"]["contract"]["task_id"] == "demo-0001"
    assert normalized_dump["changed_fields"][0]["path"] == "required_evidence"
    assert normalized_dump["artifact_paths"]["normalization_log_path"] == "runs/demo/normalization.log"

    assert refused_dump["decision"] == "refused"
    assert refused_dump["before_snapshot"]["contract"]["task_id"] == "demo-0002"
    assert refused_dump["after_snapshot"] is None
    assert refused_dump["changed_fields"] == []
    assert refused_dump["refusal_reasons"] == [
        "out_of_scope_file_changes",
        "unsafe_normalization",
    ]
    assert refused_dump["artifact_paths"]["planner_stderr_path"] == "runs/demo/planner_stderr.log"


def test_contract_normalization_outcome_refusal_reasons_forbid_unknown_values() -> None:
    with pytest.raises(ValidationError, match="refusal_reasons"):
        ContractNormalizationOutcome.model_validate(
            {
                "release_id": "demo-release",
                "task_id": "demo-0003",
                "decision": "refused",
                "rationale": "Refused for unsupported reason.",
                "before_snapshot": {"contract": _task_contract("demo-0003")},
                "refusal_reasons": ["not_a_real_reason"],
            }
        )

    request = ContractNormalizationRequest.model_validate(
        {
            "release_id": "demo-release",
            "task_id": "demo-0004",
            "rationale": "Need bounded normalization decision.",
            "before_snapshot": {"contract": _task_contract("demo-0004")},
            "artifact_paths": {"planner_prompt_path": "runs/demo/planner_prompt.md"},
        }
    )

    dumped = request.model_dump(mode="json")
    assert dumped["artifact_paths"]["planner_prompt_path"] == "runs/demo/planner_prompt.md"
    assert dumped["before_snapshot"]["contract"]["allowed_files"] == ["src/demo.py"]
    assert isinstance(Path(dumped["artifact_paths"]["planner_prompt_path"]), Path)


def test_normalize_contract_request_adds_missing_diff_and_changed_files_without_scope_change() -> None:
    contract = _task_contract("demo-1001")
    contract["required_evidence"] = ["test output log"]
    request = ContractNormalizationRequest.model_validate(
        {
            "release_id": "demo-release",
            "task_id": "demo-1001",
            "rationale": "Repair missing evidence.",
            "before_snapshot": {"contract": contract},
        }
    )

    outcome = normalize_contract_request(request)

    assert outcome.decision == ContractNormalizationDecision.NORMALIZED
    assert outcome.after_snapshot is not None
    assert outcome.after_snapshot.contract.allowed_files == request.before_snapshot.contract.allowed_files
    assert outcome.after_snapshot.contract.required_evidence == [
        "test output log",
        "git diff",
        "changed-files list",
    ]
    assert [item.path for item in outcome.changed_fields] == ["required_evidence"]


def test_normalize_task_contract_payload_repairs_one_to_one_schema_key_and_shape_drift() -> None:
    contract, changed_fields, refusal = normalize_task_contract_payload(
        {
            "task_id": "demo-1002",
            "release_id": "demo-release",
            "title": "Schema drift",
            "budget_class": "S",
            "objective": "Fix aliases.",
            "allowedFiles": ["src/demo.py"],
            "forbiddenChanges": ["Do not widen scope."],
            "requiredEvidence": ["git diff"],
            "verification": "pytest -q",
            "stopConditions": ["Stop if scope widens."],
        }
    )

    assert refusal == []
    assert contract is not None
    assert contract.allowed_files == ["src/demo.py"]
    assert contract.required_evidence == ["git diff"]
    assert contract.verification.commands == ["pytest -q"]
    assert sorted(field.path for field in changed_fields) == [
        "allowed_files",
        "forbidden_changes",
        "required_evidence",
        "stop_conditions",
        "verification",
    ]


def test_normalize_task_contract_payload_refuses_ambiguous_schema_repair() -> None:
    contract, changed_fields, refusal = normalize_task_contract_payload(
        {
            "task_id": "demo-1003",
            "release_id": "demo-release",
            "title": "Ambiguous drift",
            "budget_class": "S",
            "objective": "Reject conflicting keys.",
            "allowed_files": ["src/demo.py"],
            "allowedFiles": ["src/other.py"],
            "required_evidence": ["git diff"],
            "verification": {"commands": ["pytest -q"]},
            "stop_conditions": ["Stop if scope widens."],
        }
    )

    assert contract is None
    assert changed_fields == []
    assert refusal == [ContractNormalizationRefusalReason.AMBIGUOUS_CONTRACT_SEMANTICS]


def test_normalize_contract_request_rewrites_worktree_local_venv_only_with_safe_runtime() -> None:
    contract = _task_contract("demo-1004")
    contract["verification"] = {"commands": [".venv/bin/python -m pytest tests/test_contract_normalization.py"]}
    request = ContractNormalizationRequest.model_validate(
        {
            "release_id": "demo-release",
            "task_id": "demo-1004",
            "rationale": "Repair runtime command.",
            "before_snapshot": {"contract": contract},
        }
    )
    config = ProjectConfig.model_validate(
        {
            "project_id": "demo",
            "repo_path": ".",
            "default_base_branch": "main",
            "worktree_root": "worktrees",
            "executor": {"type": "codex_cli", "model": "worker", "max_walltime_minutes": 5},
            "verification_profiles": {
                "default": {
                    "commands": [
                        "PYTHONPATH=src /shared/.venv/bin/python -m pytest tests/test_contract_normalization.py"
                    ]
                }
            },
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        }
    )

    assert discover_safe_verification_runtime(config) == "/shared/.venv/bin/python"

    with_runtime = normalize_contract_request(request, project_config=config)
    without_runtime = normalize_contract_request(request, project_config=None)

    assert with_runtime.after_snapshot is not None
    assert with_runtime.after_snapshot.contract.verification.commands[0].startswith(
        "/shared/.venv/bin/python -m pytest"
    )
    assert any(field.path == "verification.commands[0]" for field in with_runtime.changed_fields)

    assert without_runtime.after_snapshot is not None
    assert (
        without_runtime.after_snapshot.contract.verification.commands[0]
        == ".venv/bin/python -m pytest tests/test_contract_normalization.py"
    )
    assert all(field.path != "verification.commands[0]" for field in without_runtime.changed_fields)


def test_normalize_contract_request_refuses_unsafe_worktree_python_command() -> None:
    contract = _task_contract("demo-1005")
    contract["verification"] = {"commands": [".venv/bin/python -m pytest tests/test_contract_normalization.py | tee out.log"]}
    request = ContractNormalizationRequest.model_validate(
        {
            "release_id": "demo-release",
            "task_id": "demo-1005",
            "rationale": "Repair runtime command.",
            "before_snapshot": {"contract": contract},
        }
    )
    config = ProjectConfig.model_validate(
        {
            "project_id": "demo",
            "repo_path": "/tmp/demo",
            "default_base_branch": "main",
            "worktree_root": "/tmp/worktrees",
            "verification_runtime": {"python_path": "/shared/.venv/bin/python"},
            "executor": {"type": "codex_cli", "model": "worker", "max_walltime_minutes": 5},
            "verification_profiles": {"default": {"commands": ["true"]}},
            "budget": {
                "max_executor_attempts_per_task": 1,
                "max_strong_model_calls_per_release": 1,
                "max_changed_files_per_task": 5,
                "max_diff_lines_per_task": 100,
            },
        }
    )

    outcome = normalize_contract_request(request, project_config=config)

    assert outcome.decision == ContractNormalizationDecision.REFUSED
    assert outcome.after_snapshot is None
    assert outcome.changed_fields == []
    assert outcome.refusal_reasons == [ContractNormalizationRefusalReason.UNSAFE_NORMALIZATION]


def test_is_safe_worktree_python_verification_command_detects_safe_and_unsafe_forms() -> None:
    assert is_safe_worktree_python_verification_command(".venv/bin/python -m pytest tests/test_contract_normalization.py")
    assert is_safe_worktree_python_verification_command("./.venv/bin/python -m pytest -q")
    assert not is_safe_worktree_python_verification_command("PYTHONPATH=src .venv/bin/python -m pytest")
    assert not is_safe_worktree_python_verification_command(".venv/bin/python -m pytest tests/test_contract_normalization.py | tee out.log")
