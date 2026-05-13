# Supervisor Decision Records

This document defines the typed supervisor decision records introduced by the
`supervisor-decision-records` epic.

The purpose of the record layer is narrow:

- expose judgment-heavy runtime choices as typed, auditable JSON artifacts;
- keep deterministic hard gates authoritative;
- allow soft decisions to be replayed, inspected, and tested without broad
  procedural heuristics in the release runner.

## Record Shape

Every supervisor decision record includes:

- `schema_version`
- `decision_id`
- `release_id`
- `decided_at`
- `decided_by`
- `rationale`
- `evidence_paths`
- a discriminated `decision_type`

The current version is `1.0`.

Implemented decision types:

- `release_scheduling`
- `repair_loop_continuation`
- `review_finding_adjudication`
- `soft_budget_acceptance`
- `contract_normalization`
- `environment_repair`

Each decision type adds a small typed payload that matches the judgment being
recorded. The discriminator allows strict parsing without guessing which model
should be used.

`release_scheduling` records now include:

- `selected_action` with the released scheduling choice;
- `outcome` for compatibility with the existing scheduling outcome vocabulary;
- `fallback_plan` for the next deterministic or supervisor-owned step;
- `validators_to_rerun` for the evidence checks that must be repeated if the
  decision becomes stale; and
- `staleness_inputs` capturing the selected task ids, contract paths, overlap
  report hash, base-branch commit, execution mode, and release-input hash used
  to detect stale scheduling artifacts.

## Artifact Layout

Supervisor decision artifacts are persisted as JSON files under:

```text
runs/<run-id>/<task-id>/evidence/supervisor_decisions/
runs/<run-id>/supervisor_decisions/
```

The filename format is:

```text
<decision_type>__<decision_id>.json
```

`decision_id` is sanitized to a stable filename token and rejected when it
contains path separators or `..`. That keeps the artifact path inside the
decision directory and prevents filename-based traversal.

## Loading Rules

Loading is strict:

- malformed JSON is rejected;
- unsupported `decision_type` values are rejected;
- schema validation failures are rejected;
- referenced evidence paths must exist;
- relative evidence paths may not escape the artifact directory.

Evidence validation is deliberate. A supervisor record is only useful if the
referenced files are still inspectable.

`release_scheduling` artifacts are now consumed by release execution. Normal
source overlap produces an overlap-risk report plus a typed scheduling
decision, which can serialize the release when the supervisor selects
`sequential`. Parallel execution remains available for independent tasks, and
unsupported or stale scheduling decisions fail deterministically instead of
falling back silently.

## Relationship To Hard Gates

Supervisor decisions do not bypass deterministic validators.

The kernel still owns hard rejection for:

- forbidden paths;
- generated artifacts that are out of scope;
- missing required evidence;
- unsafe finalization;
- destructive operations;
- verification failures that were not repaired.

The typed record only captures the supervisor's decision after hard gates have
already been evaluated. The record can choose among soft outcomes such as
accepting a small budget overage or retrying a bounded repair loop, but it does
not override the deterministic admission checks.

## Implemented Scope

The `soft_budget_acceptance` path is currently the first runtime consumer of
this record layer. It writes a typed supervisor decision, reloads it strictly,
and consumes the parsed record while leaving hard gates unchanged.

The `release_scheduling` path is now the second runtime consumer. It writes a
typed scheduling decision when release overlap is present, reloads it strictly,
and rejects stale or unsupported scheduling artifacts without weakening hard
path, artifact, or verification gates.

The broader multi-epic governor that would repeatedly consume these records is
still planned.
