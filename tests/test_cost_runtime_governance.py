from __future__ import annotations

import json
from pathlib import Path

from agentic_devloop.cost_runtime_governance import (
    build_cost_runtime_governance_decision,
    extract_cost_runtime_evidence_metrics,
)
from agentic_devloop.supervisor_decisions import (
    CostRuntimeGovernanceAction,
    CostRuntimeGovernanceOutcome,
)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_extract_cost_runtime_evidence_metrics_from_release_metrics(tmp_path: Path) -> None:
    metrics_path = tmp_path / "release_metrics.json"
    _write_json(
        metrics_path,
        {
            "totals": {
                "prompt_chars": 1_234_567,
                "context_chars": 456_789,
            },
            "compact_governance": {
                "review_wave_count": 4,
                "feature_review_repair_wave_count": 2,
                "model_fallback_count": 3,
            },
        },
    )

    metrics = extract_cost_runtime_evidence_metrics(release_metrics_path=metrics_path)

    assert metrics.metrics_present is True
    assert metrics.prompt_chars == 1_234_567
    assert metrics.context_chars == 456_789
    assert metrics.review_wave_count == 4
    assert metrics.repair_wave_count == 2
    assert metrics.model_fallback_count == 3


def test_build_cost_runtime_governance_decision_uses_conservative_fallback_when_metrics_absent(tmp_path: Path) -> None:
    metrics_path = tmp_path / "missing_release_metrics.json"
    tuning_path = tmp_path / "release_tuning.md"
    tuning_path.write_text("# release tuning\n", encoding="utf-8")

    decision = build_cost_runtime_governance_decision(
        decision_id="cost-runtime-governor-0003",
        release_id="cost-runtime-governor",
        decided_by="deterministic_cost_runtime_governor",
        budget_class="M",
        release_metrics_path=metrics_path,
        release_tuning_path=tuning_path,
    )

    assert decision.selected_action == CostRuntimeGovernanceAction.DECOMPOSED
    assert decision.outcome == CostRuntimeGovernanceOutcome.PROCEED_DECOMPOSED
    assert decision.selected_model_role == "balanced_worker"
    assert decision.evidence_paths == [tuning_path.resolve()]
    assert decision.validators_to_rerun == ["release_metrics", "verification", "budget_policy"]


def test_build_cost_runtime_governance_decision_routes_one_shot_from_prior_evidence(tmp_path: Path) -> None:
    metrics_path = tmp_path / "release_metrics.json"
    _write_json(
        metrics_path,
        {
            "totals": {
                "prompt_chars": 1_800_000,
                "context_chars": 920_000,
            },
            "compact_governance": {
                "review_wave_count": 5,
                "feature_review_repair_wave_count": 3,
                "model_fallback_count": 4,
            },
        },
    )

    decision = build_cost_runtime_governance_decision(
        decision_id="cost-runtime-governor-0003-one-shot",
        release_id="cost-runtime-governor",
        decided_by="deterministic_cost_runtime_governor",
        budget_class="M",
        release_metrics_path=metrics_path,
    )

    assert decision.selected_action == CostRuntimeGovernanceAction.ONE_SHOT
    assert decision.outcome == CostRuntimeGovernanceOutcome.PROCEED_ONE_SHOT
    assert decision.selected_model_role == "high_capability_worker"
    assert decision.evidence_paths == [metrics_path.resolve()]
    assert "release_metrics" in decision.validators_to_rerun
    assert "verification" in decision.validators_to_rerun
