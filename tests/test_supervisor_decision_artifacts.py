from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentic_devloop.evidence import supervisor_decisions_artifacts_dir
from agentic_devloop.supervisor_decisions import (
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
            "outcome": SchedulingOutcome.PROCEED_SEQUENTIAL,
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
