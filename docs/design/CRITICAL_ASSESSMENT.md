# Critical Assessment

## Summary

The design direction is sound: a thin external orchestrator, bounded task contracts, isolated worktrees, deterministic verification, and evidence bundles are the right primitives for pragmatic agentic development in validation-heavy repositories.

The main risk is unbounded autonomy, not autonomy itself. The system should be autonomous-first, but every autonomous step must have explicit state, evidence, budgets, rollback paths, and stopping criteria.

## Strengths

- Worktree isolation gives a practical recovery model for failed agent attempts.
- Task contracts make autonomy safer by replacing broad goals with bounded execution units.
- Evidence bundles reduce reliance on agent summaries.
- Deterministic verification protects against agents redefining success.
- Filesystem state is fast to implement and easy to inspect during early development.
- Explicit finalization flags preserve accountability for autonomous commit, merge, and push operations.

## Weaknesses

- The design still assumes agents will obey prompts unless the orchestrator enforces file, diff, and command boundaries.
- Domain validity cannot be proven by generic automation alone.
- Filesystem state will become awkward once many releases, attempts, and evidence bundles exist.
- Cost accounting may be approximate because provider CLIs often hide token-level usage.
- Subprocess isolation is not a real sandbox.
- A generic adapter interface may become too abstract before two real target repositories exist.
- The implementation currently concentrates too many responsibilities in a few large modules. `release.py` owns release coordination, scheduling, logging, summaries, metrics, cleanup, dependency analysis, and finalization. `orchestrator.py` owns task execution, model routing, verification, evidence, review, finalization, and conflict repair. This slows evolution toward a multi-epic governor.
- The current state model is artifact-scanning plus YAML files. That is inspectable, but it is not yet an authoritative state store for active epics, completed epics, retry counts, blocked work, and governor decisions.
- The system has diagnostics for failures, but not a general repair policy. Planner schema drift, verification-environment drift, flaky tests, and small worker bugs are still outside a unified autonomous repair/retry loop.
- The CLI is not a thin boundary. It wires backend construction and workflow-specific behavior that should move into application services as the command set grows.

## Architectural Refactoring Priorities

High-priority seams:

- Extract a `GovernorLoop` that owns "run the next N epics", stopping criteria, retry policy, and state refresh.
- Extract a `StateStore` API over repo-state files, run summaries, active releases, completed/blocked epics, and known learnings.
- Extract release scheduling, cockpit reporting, finalization, and metrics from `release.py`.
- Extract task execution, evidence, finalization, and repair from `orchestrator.py`.
- Add a `RepairPolicy` that maps failure categories to schema normalization, verification repair, stronger-model diagnosis, retry, or stop.

Medium-priority seams:

- Split `models.py` into configuration, contracts, runtime state, evidence, and governor schemas.
- Generalize legacy `scientific_*` naming to validation terminology with compatibility aliases.
- Move CLI backend construction into service factories.
- Define a target-repository profile for instructions, validation policy, generated artifact rules, and finalization policy.

## Pragmatic Simplifications for v1

- Support one executor backend first: Codex CLI.
- Support one target repository first: `rust_rockfall`.
- Use Python dataclasses or Pydantic models, but avoid building a plugin framework until a second adapter exists.
- Store state in predictable directories and plain files.
- Implement deterministic review before model review.
- Treat stronger-model review as the first escalation path before human interruption.
- Keep project-specific remote execution out of the control plane unless the target repository's own documentation requires it.
- Do not build automatic PR creation until evidence collection and deterministic review are stable.

## Decisions to Revisit After Sprint 0

- Whether the task contract schema is too strict or too loose.
- Whether evidence bundles contain enough information to review without rerunning commands.
- Whether the executor prompt gives agents enough context without causing drift.
- Whether worktree cleanup is safe and recoverable.
- Whether verification profiles need typed command results instead of plain logs.
- Whether run indexing needs SQLite.
- Whether a second executor backend is worth adding.
- Whether a second repository adapter is needed to validate the abstraction.

## Non-Negotiables

- No task merges or pushes itself outside configured autonomous finalization policy.
- No task skips verification.
- No domain fixture or tolerance changes without explicit permission.
- No release tagging unless configured release policy permits it.
- No secrets in logs or evidence bundles.
- No unbounded retry loops.
