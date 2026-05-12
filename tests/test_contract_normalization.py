from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agentic_devloop.models import (
    ContractNormalizationDecision,
    ContractNormalizationOutcome,
    ContractNormalizationRefusalReason,
    ContractNormalizationRequest,
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
