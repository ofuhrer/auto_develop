from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field

from agentic_devloop.models import StrictModel
from agentic_devloop.state_store import (
    EpicRefreshOutcome,
    FinalizationOutcomeReference,
    OutcomeReference,
    UnresolvedFindingReference,
)


class BacklogRunResultLike(Protocol):
    selected_epic_id: str
    release_id: str
    release: object | None
    release_summary_path: Path | None
    release_metrics_path: Path | None
    release_budget_path: Path | None
    release_tuning_path: Path | None
    finalization_policy: str | None
    finalization_result: dict[str, object] | None
    blocked_finalization: dict[str, object] | None
    governor_cycle_continuation: object | None


class PostCycleStateRefreshArtifact(StrictModel):
    captured_at: datetime
    epic_id: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    lifecycle_state: Literal["active", "completed", "blocked", "skipped", "reviewed"]
    status_reason: str = Field(min_length=1)
    release_summary_path: Path | None = None
    finalization_outcome_path: Path | None = None
    finalization_outcome_details: dict[str, object] | None = None
    review_artifact_paths: list[Path] = Field(default_factory=list)
    review_finding_summaries: list[str] = Field(default_factory=list)
    release_metrics_path: Path | None = None
    release_budget_path: Path | None = None
    release_tuning_path: Path | None = None
    retry_count: int | None = Field(default=None, ge=0)
    repair_count: int | None = Field(default=None, ge=0)
    next_recommendations: list[str] = Field(default_factory=list)
    unresolved_finding_references: list[UnresolvedFindingReference] = Field(default_factory=list)


def build_post_cycle_state_refresh(
    *,
    result: BacklogRunResultLike,
    retry_count: int | None = None,
    repair_count: int | None = None,
    now: datetime | None = None,
) -> tuple[PostCycleStateRefreshArtifact, EpicRefreshOutcome]:
    captured_at = now or datetime.now(UTC)
    summary_payload = _load_json_mapping(result.release_summary_path)
    review_path = _path_from_payload(summary_payload, "feature_review_path")
    recheck_path = _path_from_payload(summary_payload, "feature_review_recheck_path")
    finalization_outcome_path = _path_from_payload(summary_payload, "finalization_summary_path")
    if finalization_outcome_path is None:
        finalization_outcome_path = result.release_summary_path
    review_payload = _load_json_mapping(review_path)
    recheck_payload = _load_json_mapping(recheck_path)
    review_finding_summaries = _review_finding_summaries(review_payload)

    decision = _release_decision_value(result.release)
    unresolved_ids = _unresolved_finding_ids(
        blocked_finalization=result.blocked_finalization,
        recheck_payload=recheck_payload,
    )
    unresolved_refs = _unresolved_finding_references(
        unresolved_ids=unresolved_ids,
        review_payload=review_payload,
        review_path=review_path,
        recheck_path=recheck_path,
    )

    lifecycle_state, status_reason = _lifecycle_and_reason(
        decision=decision,
        blocked_finalization=result.blocked_finalization,
        finalization_result=result.finalization_result,
    )
    recommendation = _next_recommendations(
        lifecycle_state=lifecycle_state,
        unresolved_ids=unresolved_ids,
        decision=decision,
        finalization_result=result.finalization_result,
    )

    finalization_reference = _finalization_outcome_reference(
        result=result,
        summary_payload=summary_payload,
        unresolved_ids=unresolved_ids,
        lifecycle_state=lifecycle_state,
        decision=decision,
        captured_at=captured_at,
    )
    outcome_reference = OutcomeReference(
        release_id=result.release_id,
        outcome=decision,
        run_summary_path=result.release_summary_path,
        recorded_at=captured_at,
    )
    refresh_outcome = EpicRefreshOutcome(
        lifecycle_state=lifecycle_state,
        status_reason=status_reason,
        blocked_reason=_blocked_reason(result.blocked_finalization),
        retry_count=retry_count,
        repair_count=repair_count,
        next_recommendations=recommendation,
        outcome_references=[outcome_reference],
        finalization_outcome_references=[finalization_reference],
        unresolved_finding_references=unresolved_refs,
    )
    refresh_artifact = PostCycleStateRefreshArtifact(
        captured_at=captured_at,
        epic_id=result.selected_epic_id,
        release_id=result.release_id,
        lifecycle_state=lifecycle_state,
        status_reason=status_reason,
        release_summary_path=result.release_summary_path,
        finalization_outcome_path=finalization_outcome_path,
        finalization_outcome_details=_finalization_outcome_details(
            finalization_result=result.finalization_result,
            blocked_finalization=result.blocked_finalization,
            finalization_policy=result.finalization_policy,
            continuation=result.governor_cycle_continuation,
        ),
        review_artifact_paths=[path for path in [review_path, recheck_path] if path is not None],
        review_finding_summaries=review_finding_summaries,
        release_metrics_path=result.release_metrics_path,
        release_budget_path=result.release_budget_path,
        release_tuning_path=result.release_tuning_path,
        retry_count=retry_count,
        repair_count=repair_count,
        next_recommendations=recommendation,
        unresolved_finding_references=unresolved_refs,
    )
    return refresh_artifact, refresh_outcome


def write_post_cycle_state_refresh_artifact(
    *,
    artifact: PostCycleStateRefreshArtifact,
    artifacts_dir: Path,
) -> Path:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    path = artifacts_dir / "post_cycle_state_refresh.json"
    path.write_text(
        json.dumps(artifact.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def build_no_actionable_post_cycle_state_refresh(
    *,
    epic_id: str,
    release_id: str = "no_actionable_work",
    reason: str = "no_actionable_work",
    now: datetime | None = None,
) -> tuple[PostCycleStateRefreshArtifact, EpicRefreshOutcome]:
    captured_at = now or datetime.now(UTC)
    refresh_outcome = EpicRefreshOutcome(
        lifecycle_state="skipped",
        status_reason=reason,
        next_recommendations=[],
        outcome_references=[
            OutcomeReference(
                release_id=release_id,
                outcome=None,
                run_summary_path=None,
                recorded_at=captured_at,
            )
        ],
        finalization_outcome_references=[
            FinalizationOutcomeReference(
                release_id=release_id,
                outcome=None,
                run_summary_path=None,
                recommended_backlog_state="skipped",
                recorded_at=captured_at,
            )
        ],
    )
    refresh_artifact = PostCycleStateRefreshArtifact(
        captured_at=captured_at,
        epic_id=epic_id,
        release_id=release_id,
        lifecycle_state="skipped",
        status_reason=reason,
    )
    return refresh_artifact, refresh_outcome


def _release_decision_value(release: object | None) -> str | None:
    if release is None:
        return None
    raw = getattr(release, "decision", None)
    if raw is None:
        return None
    value = getattr(raw, "value", None)
    if value is not None:
        raw = value
    normalized = str(raw).strip()
    if normalized in {"accepted", "needs_revision", "failed", "escalated"}:
        return normalized
    return None


def _lifecycle_and_reason(
    *,
    decision: str | None,
    blocked_finalization: dict[str, object] | None,
    finalization_result: dict[str, object] | None,
) -> tuple[Literal["active", "completed", "blocked", "skipped", "reviewed"], str]:
    if blocked_finalization is not None:
        reason = _blocked_reason(blocked_finalization) or "finalization_blocked"
        return "blocked", f"blocked:{reason}"
    if decision == "accepted":
        finalization_payload = finalization_result or {}
        result_payload = finalization_payload.get("result")
        merged = False
        pushed = False
        if isinstance(result_payload, dict):
            merged = bool(result_payload.get("merged"))
            pushed = bool(result_payload.get("pushed"))
        if merged or pushed:
            return "completed", "accepted_and_finalized"
        return "completed", "accepted_manual_merge_or_completed"
    if decision in {"failed", "needs_revision", "escalated"}:
        return "blocked", f"release_{decision}"
    return "active", "release_in_progress_or_missing_decision"


def _blocked_reason(blocked_finalization: dict[str, object] | None) -> str | None:
    if blocked_finalization is None:
        return None
    raw = blocked_finalization.get("reason")
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _next_recommendations(
    *,
    lifecycle_state: Literal["active", "completed", "blocked", "skipped", "reviewed"],
    unresolved_ids: list[str],
    decision: str | None,
    finalization_result: dict[str, object] | None,
) -> list[str]:
    recommendations: list[str] = []
    if lifecycle_state == "blocked" and unresolved_ids:
        recommendations.append(f"resolve unresolved findings: {', '.join(unresolved_ids)}")
    if decision == "accepted":
        result_payload = (finalization_result or {}).get("result")
        merged = False
        pushed = False
        if isinstance(result_payload, dict):
            merged = bool(result_payload.get("merged"))
            pushed = bool(result_payload.get("pushed"))
        if not merged and not pushed:
            recommendations.append("record manual merge/completion outcome in repo-state memory")
    return recommendations


def _unresolved_finding_ids(
    *,
    blocked_finalization: dict[str, object] | None,
    recheck_payload: dict[str, object],
) -> list[str]:
    values: list[str] = []
    if blocked_finalization is not None:
        raw = blocked_finalization.get("unresolved_required_finding_ids")
        if isinstance(raw, list):
            values.extend(str(item).strip() for item in raw if str(item).strip())
    raw_recheck = recheck_payload.get("unresolved_finding_ids")
    if isinstance(raw_recheck, list):
        values.extend(str(item).strip() for item in raw_recheck if str(item).strip())
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _unresolved_finding_references(
    *,
    unresolved_ids: list[str],
    review_payload: dict[str, object],
    review_path: Path | None,
    recheck_path: Path | None,
) -> list[UnresolvedFindingReference]:
    finding_map: dict[str, dict[str, object]] = {}
    raw_findings = review_payload.get("findings")
    if isinstance(raw_findings, list):
        for item in raw_findings:
            if not isinstance(item, dict):
                continue
            finding_id = str(item.get("finding_id") or "").strip()
            if finding_id:
                finding_map[finding_id] = item

    references: list[UnresolvedFindingReference] = []
    for finding_id in unresolved_ids:
        finding = finding_map.get(finding_id, {})
        summary = str(finding.get("summary") or "").strip() or "unresolved finding from release review artifacts"
        severity_raw = str(finding.get("severity") or "").strip().lower()
        severity = severity_raw if severity_raw in {"low", "moderate", "high", "critical"} else None
        source_path = review_path if finding else recheck_path
        references.append(
            UnresolvedFindingReference(
                finding_id=finding_id,
                summary=summary,
                severity=severity,
                source_path=source_path,
            )
        )
    return references


def _review_finding_summaries(review_payload: dict[str, object]) -> list[str]:
    raw_findings = review_payload.get("findings")
    if not isinstance(raw_findings, list):
        return []
    summaries: list[str] = []
    for item in raw_findings:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or "").strip()
        if summary:
            summaries.append(summary)
    return summaries


def _path_from_payload(payload: dict[str, object], key: str) -> Path | None:
    raw = payload.get(key)
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return Path(value) if value else None


def _load_json_mapping(path: Path | None) -> dict[str, object]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _finalization_outcome_reference(
    *,
    result: BacklogRunResultLike,
    summary_payload: dict[str, object],
    unresolved_ids: list[str],
    lifecycle_state: Literal["active", "completed", "blocked", "skipped", "reviewed"],
    decision: str | None,
    captured_at: datetime,
) -> FinalizationOutcomeReference:
    branch = _string_from_payload(summary_payload, "integration_branch") or _string_from_payload(summary_payload, "branch")
    commit = _string_from_payload(summary_payload, "integration_commit") or _string_from_payload(summary_payload, "head_commit")
    cleanup = _path_from_payload(summary_payload, "cleanup_report_path")
    blocked_reason = _blocked_reason(result.blocked_finalization)
    blocked_type = None
    if result.blocked_finalization is not None:
        raw_type = result.blocked_finalization.get("type")
        blocked_type = str(raw_type).strip() if raw_type is not None and str(raw_type).strip() else None
    if result.blocked_finalization is not None:
        outcome = "blocked"
    else:
        outcome = decision
    recommended_state = "completed" if lifecycle_state == "completed" else "blocked" if lifecycle_state == "blocked" else "active"
    return FinalizationOutcomeReference(
        release_id=result.release_id,
        outcome=outcome,
        run_summary_path=result.release_summary_path,
        finalization_policy=result.finalization_policy,
        branch=branch,
        commit=commit,
        cleanup_report_path=cleanup,
        blocked_reason=blocked_reason,
        blocked_type=blocked_type,
        unresolved_finding_ids=unresolved_ids,
        recommended_backlog_state=recommended_state,
        recorded_at=captured_at,
    )


def _string_from_payload(payload: dict[str, object], key: str) -> str | None:
    raw = payload.get(key)
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value or None


def _finalization_outcome_details(
    *,
    finalization_result: dict[str, object] | None,
    blocked_finalization: dict[str, object] | None,
    finalization_policy: str | None,
    continuation: object | None,
) -> dict[str, object] | None:
    details: dict[str, object] = {}
    if finalization_policy is not None:
        details["finalization_policy"] = finalization_policy
    if finalization_result is not None:
        details["finalization_result"] = finalization_result
    if blocked_finalization is not None:
        details["blocked_finalization"] = blocked_finalization
    stop_reason = getattr(continuation, "stop_reason", None)
    if stop_reason is not None:
        details["continuation_stop_reason"] = str(stop_reason)
    return details or None
