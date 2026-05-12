from __future__ import annotations

import json
from pathlib import Path

import yaml

from agentic_devloop.planning import plan_release_contracts


def test_plan_release_contracts_writes_conservative_draft_when_no_contracts_exist(tmp_path) -> None:
    objective_path = tmp_path / "objective.yaml"
    _write_yaml(
        objective_path,
        {
            "release_id": "v0.2.0",
            "title": "Small release",
            "objective": "Ship one bounded increment.",
            "acceptance_criteria": ["Contract evidence exists."],
        },
    )
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()

    result = plan_release_contracts(
        objective_path=objective_path,
        contracts_dir=contracts_dir,
        runs_dir=tmp_path / "runs",
    )

    plan = json.loads(result.plan_path.read_text(encoding="utf-8"))
    assert plan["release_id"] == "v0.2.0"
    assert plan["planner"] == "deterministic"
    assert plan["generated_contracts"][0]["suggested_contract"]["task_type"] == "release_preparation"
    assert plan["warnings"]


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
