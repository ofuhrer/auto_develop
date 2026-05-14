# Architecture Reassessment

## Summary

The evidence points to a structural problem, not just a growth problem:

- the project is encoding too much agent judgment in Python,
- the hard guardrails are being used for policy choices that should be supervisor decisions,
- and the result is a brittle control plane that grows every time it learns a new exception.

The fundamental architectural mistake is trying to make the kernel do both jobs:

1. enforce real invariants, and
2. simulate the agent’s judgment.

That is why code volume and complexity keep rising. Every new “safe” rule becomes another enum, validator, repair path, artifact, and test. The system spends increasing effort on avoiding its own guardrails.

## Core Diagnosis

The architecture is too deterministic in the wrong places.

What should stay in code:

- Git and worktree safety,
- subprocess execution,
- artifact persistence,
- path and file safety,
- verification execution,
- schema checks for a small set of core records,
- and finalization mechanics.

What should move up into prompts, config, or supervisor decisions:

- epic selection,
- backlog prioritization,
- one-shot versus decomposed execution,
- repair versus rerun versus split,
- finalization preference,
- soft scope-risk handling,
- and review adjudication.

The issue is not that the code has invariants. The issue is that it has too many *policy* invariants. That turns normal agent judgment into hard-coded branching logic, which is exactly the kind of logic that keeps generating follow-up code.

## What The Run Evidence Suggests

The run evidence is consistent with the same architectural pressure:

1. Durable state is too sticky. An epic can remain “active” in memory and become ineligible again, even when the planner has selected it correctly.
2. Finalization policy is not cleanly owned. The CLI can request one landing mode while config policy silently pushes another.
3. Hard gates are absorbing soft judgments. Contract-size limits, scope overages, and some repair-classification cases are being treated as deterministic blockers when they should be supervisor-adjudicated.
4. Logging is phase-local. There is a governor log and per-release logs, but not a single run-level cockpit stream that follows the whole multi-cycle execution.
5. Self-hosting is coupled to target-repo state. The control plane still leaks planning/package artifacts into the target checkout in ways that create dirty-repo failures.

These are symptoms of the same underlying issue: the kernel is doing too much reasoning.

## Revised Recommendation

The best way to bring down complexity and code volume is to reduce the amount of policy encoded in Python.

### 1. Make the kernel thinner

Keep only strict invariants in code:

- filesystem and worktree safety,
- verification,
- artifact writing,
- and schema validation for durable state.

Everything else should be treated as a policy decision, not a kernel rule.

### 2. Give the agents more authority

The supervisor should decide:

- which epic to run next,
- whether to split or keep a task cohesive,
- whether a size overage is acceptable,
- whether a finding is blocker-worthy or advisory,
- and which finalization path to use.

The code should present facts, not simulate the full judgment process.

### 3. Collapse policy code into prompts and generic records

The current pattern is:

- encode a policy in Python,
- validate it,
- repair it,
- persist it,
- then add tests for the repair path.

That pattern should be reversed.

Use prompts and structured agent output for policy decisions. Keep code responsible for parsing and validating a smaller set of stable envelopes. This removes the need for many bespoke enums, record types, and repair helpers.

### 4. Reclassify soft limits

Budgets like changed-files count, diff-size, and context size should usually be soft findings, not hard stops.

Only keep hard stops for actual unsafe conditions:

- forbidden files,
- generated artifacts,
- migrations,
- lockfiles,
- missing evidence,
- and failed verification.

If a limit is operational, route it to the supervisor. If it is a safety boundary, keep it deterministic.

### 5. Simplify the state model

The durable state should not require a human to manually repair stale `active_epics` entries just to continue the loop.

Use a smaller state model:

- one active lease,
- a compact completed/blocked record,
- and a short run summary trail.

That is enough for restartability without building a large memory subsystem that can get out of sync with reality.

### 6. Unify the run log

There should be one human-facing cockpit log for the whole governor run, with child release progress stitched into it.

Raw per-release logs can remain for audit, but the primary live view should be continuous across cycles. Right now the log structure mirrors the architectural fragmentation.

## Highest-ROI Simplifications

1. Shrink `models.py` and `runtime_supervisor.py` by replacing many specialized decision types with a smaller generic decision envelope.
2. Reduce `feature_review.py` and `planning.py` to prompt assembly, parsing, and minimal compatibility normalization.
3. Trim `release.py` so it coordinates phases instead of implementing policy for each phase.
4. Replace phase-specific artifact sprawl with one run manifest plus a few optional phase records.
5. Cut tests that only prove internal policy branches once those branches disappear.

## Patterns To Borrow

The inspiration here is not “more harness.” It is the opposite: make the system smaller and let the agent own more of the policy.

### From `autoresearch`

Borrow the strongest simplification principle:

- keep the repo tiny,
- let the agent edit one or two real files,
- use one primary metric,
- and keep the loop easy to understand.

The architectural lesson is that a small stable kernel plus a single agent-editable instruction file is often enough. If the agent can directly own the main decision surface, the harness does not need to keep simulating policy in code.

### From `autonomous-dev`

Borrow the project-level policy idea:

- put goals, scope, constraints, and architecture into a project manifest,
- have the agent read that before work,
- and block work that is clearly out of scope.

That part is useful because it externalizes policy. But do not copy the large hook/command/agent surface. That would reintroduce the same complexity problem in a different package.

### Combined lesson

The best target shape is:

- a tiny deterministic kernel,
- one project-level policy file,
- one agent instruction file,
- and a small number of structured decision envelopes.

That is much closer to `autoresearch` than to a large enforcement-heavy harness.

## Bottom Line

This project will not get smaller by adding more rules. It will get smaller by moving more judgment out of code and into agents.

The right architecture is:

- a strict deterministic kernel for real invariants,
- a supervisor that owns policy and tradeoffs,
- and a compact record model that avoids encoding every exception as a new class and a new validator.

That is the path to lower complexity, lower code volume, and less brittleness.

## Aggressive Work Plan

This plan assumes we are willing to accept temporary instability, broken tests, and incomplete workflows while we move the architecture in the right direction.

### Phase 1: Stop adding policy code

1. Freeze new deterministic policy branches unless they are true safety invariants.
2. Reject new enums, validators, and bespoke repair paths unless they replace at least as much code as they add.
3. Default to prompt changes, supervisor decisions, or smaller generic schemas instead of new phase-specific logic.
4. Add a project-level policy manifest and instruction file if they are missing, and make them the first place where policy lives.

### Phase 2: Collapse the policy surface

1. Replace specialized decision artifacts with one generic decision envelope.
2. Remove duplicate normalization logic across planning, review, runtime supervision, and finalization.
3. Move soft-budget handling out of hard-stop code paths and into supervisor-adjudicated findings.
4. Simplify the durable state model to a single active lease plus compact outcome records.
5. Move scope, goals, and constraints out of code and into a project manifest that the supervisor and agents read directly.

### Phase 3: Hand more authority to agents

1. Make the supervisor choose epic selection, scope splits, repair strategy, and finalization mode.
2. Put review interpretation and ambiguity handling into prompts and structured outputs.
3. Treat the kernel as an executor and validator, not as a hidden policy engine.
4. Prefer a failed or degraded autonomous decision over a large amount of brittle fallback code.
5. Make the agent instructions the canonical place for “how to behave,” and keep Python mostly as transport and validation.

### Phase 4: Remove brittle guardrails

1. Downgrade operational thresholds such as context size and task size from hard blockers to soft findings unless they are actual safety boundaries.
2. Eliminate hard-coded special cases that exist only to recover from previous hard-coded special cases.
3. Allow some cycles to fail visibly while the new prompt-driven policy is being learned and stabilized.
4. Keep only hard gates that protect against irreversible damage, unsafe paths, and failed verification.

### Phase 5: Rebuild around a smaller kernel

1. Keep only the minimum deterministic primitives needed for safe execution.
2. Reintroduce policy only where a prompt-based supervisor cannot represent it cleanly.
3. Measure success by fewer code paths, fewer record types, fewer repair modes, and fewer tests.
4. Accept that the transitional version may be less stable before it becomes simpler and more maintainable.
5. Prefer one or two stable editable files over many phase-specific policy modules.

### Execution Order

1. Start with state, logging, and finalization precedence, because those create the most visible brittleness.
2. Then move policy out of `planning.py`, `feature_review.py`, and `release.py` into prompts and project manifests.
3. Then simplify `models.py` and `runtime_supervisor.py`.
4. Then cut artifact sprawl and trim tests around removed behavior.
5. Finally, collapse the remaining orchestration glue so the kernel is clearly thinner than the agent policy layer.

### Success Criteria

1. Fewer specialized Python branches for policy decisions.
2. Fewer artifact types and fewer interdependent state files.
3. More explicit agent ownership of judgment-heavy choices.
4. A smaller, less brittle kernel that only enforces what truly must be deterministic.
5. Lower LOC and lower test volume as a direct outcome of removing policy from code.
6. A project policy file and agent instruction file that carry the bulk of the behavior.
