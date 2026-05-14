from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from agentic_devloop.supervisor_decisions import (
    CostRuntimeGovernanceAction,
    CostRuntimeGovernanceDecision,
    CostRuntimeGovernanceOutcome,
    DecisionRiskLevel,
    SupervisorDecisionType,
)

_DEFAULT_VALIDATORS_TO_RERUN = ["release_metrics", "verification", "budget_policy"]


@dataclass(frozen=True)
class CostRuntimeEvidenceMetrics:
    metrics_present: bool
    prompt_chars: int
    context_chars: int
    review_wave_count: int
    repair_wave_count: int
    model_fallback_count: int


def extract_cost_runtime_evidence_metrics(*, release_metrics_path: Path | None) -> CostRuntimeEvidenceMetrics:
    if release_metrics_path is None:
        return _empty_metrics()
    payload = _read_json_mapping(release_metrics_path)
    if payload is None:
        return _empty_metrics()

    totals = _mapping(payload.get("totals"))
    compact = _mapping(payload.get("compact_governance"))

    return CostRuntimeEvidenceMetrics(
        metrics_present=True,
        prompt_chars=_int_value(totals.get("prompt_chars")),
        context_chars=_int_value(totals.get("context_chars")),
        review_wave_count=_int_value(compact.get("review_wave_count")),
        repair_wave_count=_int_value(compact.get("feature_review_repair_wave_count")),
        model_fallback_count=_int_value(compact.get("model_fallback_count")),
    )


def build_cost_runtime_governance_decision(
    *,
    decision_id: str,
    release_id: str,
    decided_by: str,
    budget_class: str,
    release_metrics_path: Path | None,
    release_tuning_path: Path | None = None,
    decided_at: datetime | None = None,
) -> CostRuntimeGovernanceDecision:
    metrics = extract_cost_runtime_evidence_metrics(release_metrics_path=release_metrics_path)

    selected_action: CostRuntimeGovernanceAction
    outcome: CostRuntimeGovernanceOutcome
    selected_model_role: str
    risk_level: DecisionRiskLevel
    rationale: str

    if not metrics.metrics_present:
        selected_action = CostRuntimeGovernanceAction.DECOMPOSED
        outcome = CostRuntimeGovernanceOutcome.PROCEED_DECOMPOSED
        selected_model_role = "balanced_worker"
        risk_level = DecisionRiskLevel.HIGH
        rationale = (
            "No structured release_metrics evidence was available; applying conservative fallback "
            "to decomposed execution with a balanced model route."
        )
    else:
        high_context_pressure = metrics.prompt_chars >= 1_000_000 or metrics.context_chars >= 800_000
        high_review_churn = metrics.review_wave_count >= 3 or metrics.repair_wave_count >= 2
        high_model_fallback = metrics.model_fallback_count >= 3

        if budget_class in {"XS", "S"}:
            selected_action = CostRuntimeGovernanceAction.DECOMPOSED
            outcome = CostRuntimeGovernanceOutcome.PROCEED_DECOMPOSED
            selected_model_role = "cost_efficient_worker"
            risk_level = DecisionRiskLevel.MODERATE
            rationale = (
                "Budget class is constrained (XS/S); route to decomposed execution with a "
                "cost-efficient worker model to bound spend risk."
            )
        elif high_review_churn and (high_context_pressure or high_model_fallback):
            selected_action = CostRuntimeGovernanceAction.ONE_SHOT
            outcome = CostRuntimeGovernanceOutcome.PROCEED_ONE_SHOT
            selected_model_role = "high_capability_worker"
            risk_level = DecisionRiskLevel.MODERATE
            rationale = (
                "Prior evidence indicates review churn and high context/fallback pressure; "
                "route to one-shot high-capability execution to reduce repair/review loops."
            )
        elif high_review_churn:
            selected_action = CostRuntimeGovernanceAction.REVIEW_CAPPED
            outcome = CostRuntimeGovernanceOutcome.PROCEED_REVIEW_CAPPED
            selected_model_role = "balanced_worker"
            risk_level = DecisionRiskLevel.MODERATE
            rationale = (
                "Prior evidence indicates elevated review churn; route through review-capped mode "
                "to limit repeated reviewer waves."
            )
        else:
            selected_action = CostRuntimeGovernanceAction.DECOMPOSED
            outcome = CostRuntimeGovernanceOutcome.PROCEED_DECOMPOSED
            selected_model_role = "balanced_worker"
            risk_level = DecisionRiskLevel.LOW
            rationale = (
                "Recent metrics are within normal thresholds; keep decomposed execution with a "
                "balanced worker route."
            )

    evidence_paths: list[Path] = [
        path
        for path in [release_metrics_path, release_tuning_path]
        if path is not None
    ]

    return CostRuntimeGovernanceDecision.model_validate(
        {
            "decision_type": SupervisorDecisionType.COST_RUNTIME_GOVERNANCE,
            "decision_id": decision_id,
            "release_id": release_id,
            "decided_at": (decided_at or datetime.now(UTC)),
            "decided_by": decided_by,
            "rationale": rationale,
            "evidence_paths": evidence_paths,
            "risk_level": risk_level,
            "selected_action": selected_action,
            "outcome": outcome,
            "selected_model_role": selected_model_role,
            "budget_class": budget_class,
            "fallback_plan": "Re-run verification and budget validation before applying alternate routing.",
            "validators_to_rerun": list(_DEFAULT_VALIDATORS_TO_RERUN),
        }
    )


def _read_json_mapping(path: Path) -> Mapping[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _int_value(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _empty_metrics() -> CostRuntimeEvidenceMetrics:
    return CostRuntimeEvidenceMetrics(
        metrics_present=False,
        prompt_chars=0,
        context_chars=0,
        review_wave_count=0,
        repair_wave_count=0,
        model_fallback_count=0,
    )
