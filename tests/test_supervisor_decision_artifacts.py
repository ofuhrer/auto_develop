from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentic_devloop.evidence import supervisor_decisions_artifacts_dir
from agentic_devloop.supervisor_decisions import (
    ReleaseSchedulingAction,
    SCHEMA_VERSION_V1,
    DecisionRiskLevel,
    ReleaseSchedulingDecision,
    SchedulingOutcome,
    SupervisorDecisionType,
    load_supervisor_decision_artifact,
    supervisor_decision_artifact_path,
    write_supervisor_decision_artifact,
)


def _decision(*, evidence_paths: list[Path]) -> ReleaseSchedulingDecision:
    return ReleaseSchedulingDecision.model_validate(
        {
            "schema_version": SCHEMA_VERSION_V1,
            "decision_id": "schedule-001",
            "release_id": "supervisor-decision-records",
            "decided_at": datetime(2026, 5, 13, 8, 0, 0),
            "decided_by": "supervisor-agent",
            "rationale": "Serialized overlap findings indicate sequential execution.",
            "evidence_paths": evidence_paths,
            "decision_type": SupervisorDecisionType.RELEASE_SCHEDULING,
            "risk_level": DecisionRiskLevel.MODERATE,
            "overlap_findings": ["src/agentic_devloop/release.py"],
            "selected_action": ReleaseSchedulingAction.SEQUENTIAL,
            "outcome": SchedulingOutcome.PROCEED_SEQUENTIAL,
            "fallback_plan": "Rerun overlap analysis before resuming parallel execution.",
            "validators_to_rerun": ["overlap_report", "verification"],
            "staleness_inputs": {
                "execution_mode": "parallel",
                "selected_task_ids": ["demo-0001"],
                "selected_contract_paths": [str(Path("contract.yaml"))],
                "overlap_report_sha256": "abc123",
                "base_branch_head_commit": "deadbeef",
                "release_inputs_sha256": "f00d",
            },
        }
    )


def test_supervisor_decision_artifact_path_is_deterministic(tmp_path: Path) -> None:
    path = supervisor_decision_artifact_path(
        release_bundle_path=tmp_path,
        decision_type=SupervisorDecisionType.REPAIR_LOOP_CONTINUATION,
        decision_id="repair-003",
    )

    assert path == tmp_path / "supervisor_decisions" / "repair_loop_continuation__repair-003.json"
    assert supervisor_decisions_artifacts_dir(tmp_path) == tmp_path / "supervisor_decisions"


@pytest.mark.parametrize("decision_id", ["../escape", "nested/path", r"nested\path", "safe..but-bad"])
def test_supervisor_decision_artifact_path_rejects_path_like_decision_ids(
    tmp_path: Path, decision_id: str
) -> None:
    with pytest.raises(ValueError, match="path separators"):
        supervisor_decision_artifact_path(
            release_bundle_path=tmp_path,
            decision_type=SupervisorDecisionType.REPAIR_LOOP_CONTINUATION,
            decision_id=decision_id,
        )


def test_supervisor_decision_artifact_path_sanitizes_filename_token(tmp_path: Path) -> None:
    path = supervisor_decision_artifact_path(
        release_bundle_path=tmp_path,
        decision_type=SupervisorDecisionType.REPAIR_LOOP_CONTINUATION,
        decision_id="repair 003:retry",
    )

    assert path == tmp_path / "supervisor_decisions" / "repair_loop_continuation__repair_003_retry.json"


def test_write_and_load_supervisor_decision_artifact_round_trip(tmp_path: Path) -> None:
    evidence_file = tmp_path / "verification.log"
    evidence_file.write_text("ok\n", encoding="utf-8")
    decision = _decision(evidence_paths=[evidence_file])

    artifact_path = write_supervisor_decision_artifact(
        release_bundle_path=tmp_path,
        decision=decision,
    )
    loaded = load_supervisor_decision_artifact(artifact_path)

    assert artifact_path.exists()
    assert loaded == decision


def test_load_supervisor_decision_artifact_fails_when_missing(tmp_path: Path) -> None:
    missing = tmp_path / "supervisor_decisions" / "missing.json"

    with pytest.raises(FileNotFoundError, match="does not exist"):
        load_supervisor_decision_artifact(missing)


def test_load_supervisor_decision_artifact_fails_on_invalid_payload(tmp_path: Path) -> None:
    artifact_path = tmp_path / "supervisor_decisions" / "invalid.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text('{"decision_type": "release_scheduling"}\n', encoding="utf-8")

    with pytest.raises(ValidationError):
        load_supervisor_decision_artifact(artifact_path)


def test_load_supervisor_decision_artifact_fails_for_missing_evidence_path(tmp_path: Path) -> None:
    decision = _decision(evidence_paths=[Path("missing-evidence.log")])
    artifact_path = write_supervisor_decision_artifact(
        release_bundle_path=tmp_path,
        decision=decision,
    )

    with pytest.raises(ValueError, match="missing evidence path"):
        load_supervisor_decision_artifact(artifact_path)


def test_load_supervisor_decision_artifact_rejects_relative_evidence_path_traversal(
    tmp_path: Path,
) -> None:
    escaped_evidence = tmp_path / "escape.log"
    escaped_evidence.write_text("outside bundle\n", encoding="utf-8")
    artifact_path = tmp_path / "supervisor_decisions" / "traversal.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION_V1,
                "decision_id": "traversal",
                "release_id": "supervisor-decision-records",
                "decided_at": "2026-05-13T08:00:00",
                "decided_by": "supervisor-agent",
                "rationale": "Traversal should be rejected.",
                "evidence_paths": ["../escape.log"],
                "decision_type": "release_scheduling",
                "risk_level": "moderate",
                "overlap_findings": [],
                "selected_action": "sequential",
                "outcome": "proceed_sequential",
                "fallback_plan": "Rerun overlap analysis before resuming parallel execution.",
                "validators_to_rerun": ["overlap_report", "verification"],
                "staleness_inputs": {
                    "execution_mode": "parallel",
                    "selected_task_ids": ["demo-0001"],
                    "selected_contract_paths": [str(Path("contract.yaml"))],
                    "overlap_report_sha256": "abc123",
                    "base_branch_head_commit": "deadbeef",
                    "release_inputs_sha256": "f00d",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="escapes artifact directory"):
        load_supervisor_decision_artifact(artifact_path)
