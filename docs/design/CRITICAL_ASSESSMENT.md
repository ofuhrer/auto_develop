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
- The implementation now exposes a one-epic `GovernorLoop` boundary, a typed `StateStore` seam, a `RepairPolicy` seam, typed supervisor decision records, planner-output normalization, and one-epic governor logging, but the broader multi-epic governor still has to prove those boundaries across repeated cycles.
- The soft-gate implementation now records accepted exceptions as evidence-backed artifacts, but the broader multi-epic governor automation that would consume them across repeated cycles remains planned.
- The current state model is still intentionally narrow. The typed `StateStore` seam improves persistence discipline, but the longer-running backlog memory for active epics, completed epics, retry counts, blocked work, and governor decisions still needs the full multi-epic loop.
- The system now has runtime-supervisor repair/resume seams plus deterministic state-review snapshot capture and contract-plan snapshot-path plumbing. It still lacks full agent-driven pre-epic state-review decisioning and an independent feature-review loop. The deterministic kernel can execute and summarize releases, but it does not yet make backlog selection from a complete live source/branch/artifact state decision pass or review the integrated feature branch as a coherent PR-style change set. Typed supervisor decision records are the implemented bridge for recovery and soft decisions; the N-epic loop that would orchestrate them repeatedly remains planned.
- The CLI is not a thin boundary. It wires backend construction and workflow-specific behavior that should move into application services as the command set grows.

## Architectural Refactoring Priorities

High-priority seams:

- Expand the state-review governor service before backlog selection from snapshot capture into full decisioning. It should collect branch/source/doc/run/review/metric/tuning evidence, persist snapshots, and drive epic selection from that live state.
- Add an independent feature-review service after feature-branch integration. It should assemble a review packet, call a reviewer agent, validate structured findings, create bounded repair contracts, rerun verification, and re-check unresolved findings.
- Extend the `StateStore` API over repo-state files, run summaries, active releases, completed/blocked epics, and known learnings into authoritative multi-epic state.
- Extend the `GovernorLoop` from the current one-epic service boundary to a multi-epic "run the next N epics" loop with stopping criteria, retry policy, runtime supervision, feature review, and state refresh.
- Extract release scheduling, cockpit reporting, finalization, and metrics from `release.py`.
- Extract task execution, evidence, finalization, and repair from `orchestrator.py`.
- Extend `RepairPolicy` so it can map failure categories to executable repair actions: schema normalization, contract splitting, scope narrowing, environment repair, long-running-worker inspection, stronger-model diagnosis, retry, or stop across repeated epic cycles.

Medium-priority seams:

- Split `models.py` into configuration, contracts, runtime state, evidence, and governor schemas.
- Generalize legacy `scientific_*` naming to validation terminology with compatibility aliases.
- Move CLI backend construction into service factories.
- Define a target-repository profile for instructions, validation policy, generated artifact rules, and finalization policy.

## Candidate Code Reduction Through Agentic Supervision

The project should not replace hard safety gates with model judgment. It can,
however, remove or shrink procedural code that currently tries to approximate
judgment, diagnosis, prioritization, or repair. Those regions are expensive to
maintain because they accumulate special cases from every dogfood run.

Keep as deterministic code:

- Git worktree/branch creation, cleanup, merge locks, and finalization policy.
- Contract schema validation and hard admission checks.
- Verification command execution and result capture.
- Evidence bundle writing and immutable artifact paths.
- Budget counters and hard budget enforcement.
- Secret, destructive-operation, and policy-boundary checks.

Refactor toward runtime-supervisor decisions:

- Deterministic backlog extraction and scoring in `backlog.py`. Keep deterministic
  mode only as a small test fixture/fallback; let the governor agent own epic
  discovery and prioritization from docs, repo-state, and run artifacts.
- Heuristic generated-contract quality checks in `planning.py`, especially
  wording-based checks such as "must include a scope or verification stop
  condition." Keep the safety admission gate, but route semantically useful
  invalid contracts to a contract-normalization repair action instead of
  growing string heuristics.
- File-overlap response policy in `release.py`. Keep overlap detection as a hard
  signal, but let the runtime supervisor decide whether to serialize, split,
  narrow scope, or replan contracts instead of encoding every recovery strategy
  in scheduler code.
- Deterministic failure classification in `failure_diagnosis.py`. Keep evidence
  collection and typed categories, but replace brittle log-pattern diagnosis
  with a supervisor-backed diagnostic backend that can inspect evidence and
  choose a bounded repair action.
- Budget and tuning prose in `budget.py`. Keep numeric ledgers and budget
  enforcement, but let the supervisor synthesize tuning recommendations,
  task-size adjustments, model-routing changes, and next-run contract splits.
- Human cockpit formatting and worker-summary filtering in `release.py`. Keep
  raw event emission and audit logs, but make curated human/supervisor summaries
  data-driven so formatting does not grow into another policy engine.
- Environment-specific preflight repair around editable installs, venv paths,
  and `PYTHONPATH`. Keep `doctor` diagnostics deterministic, but let the
  supervisor execute bounded environment-repair recipes before declaring a
  release blocked.

Recent dogfood runs make this refactor urgent. Deterministic overlap gates
blocked a usable release package because multiple contracts touched
`release.py`; a human then made the obvious scheduling decision to run slices
sequentially. That judgment should be a typed supervisor decision, not a manual
escape hatch and not more scheduler branches. Likewise, the feature-review loop
can find valuable repairs across several rounds, but without convergence policy
it can also keep expanding scope until retry budget exhaustion. The supervisor
needs explicit authority to continue, stop, accept with rationale, or defer to
backlog based on finding class and verification evidence.

The target shape is a smaller deterministic kernel plus explicit agent-facing
tools. The kernel emits facts and enforces invariants; the runtime supervisor
interprets facts, proposes repairs, applies approved bounded actions, and reruns
the kernel.

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
