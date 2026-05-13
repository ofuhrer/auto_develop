# Supervisor Decision Schemas

This document defines the typed, versioned supervisor decision records introduced for `supervisor-decision-records`.

Status: schema models and deterministic artifact IO are implemented. The runtime currently emits and reloads
`soft_budget_acceptance` decision artifacts for task-level soft-budget exceptions. Other decision types are
schema-ready but not yet wired into runtime control flow.

## Versioning

All decision records currently use:

- `schema_version: "1.0"`

The models reject unknown schema versions to keep downstream handling explicit and auditable.

## Common Record Fields

Every supervisor decision record includes:

- `schema_version`
- `decision_id`
- `release_id`
- `decided_at`
- `decided_by`
- `rationale`
- `evidence_paths`

These fields capture identity, timing, provenance, and evidence linkage for all decision classes.

## Decision Types (v1)

`decision_type` is the discriminator for the union schema.

1. `release_scheduling`
- Purpose: choose scheduling posture after overlap/risk analysis.
- Key fields: `risk_level`, `overlap_findings`, `outcome`.
- Outcomes: parallel, sequential, stacked branches, replan, stop.

2. `repair_loop_continuation`
- Purpose: choose whether repair/retry continues.
- Key fields: `risk_level`, `attempt`, `max_attempts`, `outcome`.
- Hard guard: `attempt` must be `<= max_attempts`.

3. `review_finding_adjudication`
- Purpose: adjudicate reviewer findings.
- Key fields: `finding_id`, `severity`, `outcome`, `repair_task_ids`.
- Outcomes: required repair, accepted risk, false positive, out-of-scope follow-up.

4. `soft_budget_acceptance`
- Purpose: decide handling for soft budget pressure.
- Key fields: `budget_name`, `configured_limit`, `actual`, `outcome`.
- Hard guard: `actual` must be `>= configured_limit`.

5. `contract_normalization`
- Purpose: record normalization vs refusal decisions on generated contracts.
- Key fields: `outcome`, `changed_fields`, `refusal_reasons`.
- Hard guards:
  - `normalize_and_retry` requires non-empty `changed_fields`.
  - `refuse_and_stop` requires non-empty `refusal_reasons`.

6. `environment_repair`
- Purpose: record decisions for environment repair/capture actions.
- Key fields: `outcome`, `capture_commands`.
- Outcomes: apply-and-retry, capture-only, escalate, stop.

## Design Constraints

- Strict parsing is enabled (`extra="forbid"`) via shared `StrictModel` conventions.
- Decision artifacts are written below `supervisor_decisions/` using sanitized filename tokens; path-like decision IDs are rejected.
- Loading artifacts validates both schema shape and referenced evidence paths.
- Deterministic hard validators remain authoritative; supervisor decisions record and bound soft judgment but do not override hard gates.
