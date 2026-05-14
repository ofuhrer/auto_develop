# Known Failures

## Experiment learning: one-shot versus decomposed execution

- The `supervisor-owned-release-scheduling` experiment compared a frozen
  auto_develop-generated five-contract package with a one-shot high-capability
  Codex implementation from the same base commit.
- The one-shot branch changed the same 13 files, passed the same full test
  suite, and produced a more coherent scheduling schema with explicit action
  enums, structured staleness inputs, and action/outcome consistency checks.
- The auto_develop run accepted all five worker tasks with no retries and
  cleaned worktrees/agent branches correctly, but finalization was blocked
  because useful semantic reviewer output failed strict schema validation on
  empty `evidence_paths`.
- Architectural consequence: the governor must choose execution strategy before
  contract generation. One-shot implementation is the right default for
  cohesive medium architectural work; decomposed contracts are useful when work
  is independent, risky, needs isolation, or benefits from parallelism.
- Raw planner/reviewer output should be normalized or repaired before strict
  typed validation hard-stops execution. Strict schemas remain valuable after
  normalization.
- This finding does not justify a rewrite. Keep the deterministic kernel for
  Git/worktrees, hard gates, verification, evidence, metrics, typed artifact
  persistence, and finalization. Move judgment-heavy orchestration behind a
  supervisor/governor boundary.

## Closed-loop run learning: unsupported primary worker

- The first full release run exposed account limits for `gpt-5.3-codex-spark`.
- Current project configs now treat Spark as the preferred worker and repair
  choice with safe fallbacks to `gpt-5.3-codex` and `gpt-5.4-mini`.

## Current Gap: autonomous roadmap governor

- `plan-backlog --mode strong-model --execute-planner` can invoke a governor
  agent that reads docs, roadmap, repo-state memory, and goals, then emits a
  validated `BacklogPlan`.
- `run-backlog` now chains selected-epic backlog planning into objective,
  contract, and release execution for one epic.
- Runtime supervision, planner-output normalization, persistent governor memory
  seams, typed supervisor decision records, and one-epic governor logging are now
  partially implemented. The
  remaining governor gap is the high-level loop around them: before selecting
  the next epic, the governor should inspect live repository state, branch
  state, source layout, recent run artifacts, release reviews, metrics,
  tuning reports, unresolved findings, and tracked repo-state memory.
- The current governor can select one epic and persist bounded state-review
  snapshots, but it does not yet run a full agent-driven state review before
  prioritization or loop over the next N highest-reward epics.

## Current Gap: one-shot worker execution

- The `supervisor-execution-strategy` dogfood run shipped the typed
  execution-strategy seam and bounded `one_shot_execution_input.json`
  materialization.
- It does not yet execute that one-shot input with a worker, verification,
  evidence bundle, review, and feature-branch finalization.
- Until the one-shot runner exists, `run-backlog` should keep executable
  contract decomposition as its default; explicit `one_shot` selection is a
  planning/input artifact path and may return `release: null`.
- The next strategy increment should implement a one-shot worker runner instead
  of continuing to describe one-shot as fully executable.

## Current Gap: execution-strategy stop artifact

- `stop` execution-strategy outcomes are currently recorded in
  `execution_strategy_selection.json` only.
- Typed `execution_strategy` supervisor decision artifacts are written for
  executable and replanning actions, but not for blocked `stop` outcomes.
- The target design should add typed blocked-decision persistence so all
  strategy outcomes have one uniform supervisor-decision trail.

## Current Gap: review-loop convergence

- `release_review.md` is a deterministic evidence summary; semantic review now
  lives in `feature_review.json` and `feature_review_recheck.json` when a
  `model_roles.reviewer` is configured.
- The `agentic-feature-review-loop` dogfood run showed that the semantic review
  loop can produce useful repair contracts and also false-positive required
  findings after verification already proves the concern false.
- Required-finding adjudication is now deliberately narrow: verification-only or
  conditional findings can be accepted with rationale after the configured
  integration verification rerun passes; findings that require implementation
  changes remain blocking.
- Remaining gap: this adjudication is still local to one release and lacks
  convergence policy for repeated adjacent findings. The broader governor should
  own typed evidence-backed reviewer/supervisor decisions rather than
  accumulating procedural heuristics in `release.py`.

## Dogfood learning: planner-normalization-generalization cycle

- The `planner-normalization-generalization` cycle shipped the reusable typed
  planner-output normalization path, but exposed failures in the top-level loop
  around the release runner.
- Tracked repo-state was stale after a manual merge and still listed a completed
  epic as active. This can cause the governor to reselect completed work. The
  governor must own state refresh after both autonomous finalization and manual
  merges.
- Self-development dirties the target checkout with generated objectives,
  contracts, and repo-state before execution. This is expected when the
  controller repo is also the target repo, but the governor should treat the
  planning package commit as a normal policy-owned step. External targets should
  keep durable `.auto_develop/` artifacts in the target repo or a control repo,
  while raw runs remain local/audit-only.
- The human still had to correlate `governor.log`, `release.log`, raw logs,
  release summaries, and review artifacts. The product target is one
  tail-able, human-facing `governor.log` cockpit for the full N-epic run, with
  child release logs summarized into it.
- The generated plan contained one contract over the hard file-count budget.
  Hard file-count limits are too crude for legitimate mechanical many-file
  changes. File/diff budgets should produce typed scope-risk findings that a
  supervisor classifies as mechanical, cohesive, risky, or scope creep before
  deterministic validators rerun.
- The semantic reviewer produced multiple repair waves and then new adjacent
  findings at the convergence limit. After bounded repair attempts, the
  reviewer should stop; a supervisor should run final integration verification
  and accept, defer, or block remaining findings with typed rationale.
- A real missed integration issue was still caught by the final full suite
  after the release stopped. The target design should keep full integration
  verification as a hard gate, but move final finding adjudication from human
  judgment into a typed supervisor decision.

## Closed-loop run learning: run-backlog autonomous loop

- A full step-by-step dogfood run successfully merged the
  `run-backlog-autonomous-loop` feature into `main`.
- `plan-backlog` produced a useful selected epic and objective. Strong-model
  `plan-release` then produced semantically useful task decomposition but
  schema-invalid contract JSON. The system should repair or normalize such
  planner output autonomously before stopping.
- Single-contract continuation initially failed because dependencies were only
  validated against the current invocation. Release continuation now reads
  prior accepted and merged task ids from matching `release_summary.json`
  artifacts.
- Worktree verification failed when project config used the main checkout's
  virtualenv without forcing `PYTHONPATH=src`. The `auto_develop` config now
  makes worktree-local imports explicit.
- Verification evidence for failures was too thin: `verification.log` captured
  exit code, timeout, and duration, but not enough command stdout/stderr to
  diagnose pytest failures without reapplying patches manually.
- Worker summaries often reported that tests could not run inside isolated
  worker sandboxes, while orchestrator verification could run them from the
  configured environment. This is acceptable, but the cockpit log should make
  the distinction explicit.
- One worker produced a small dataclass field-order bug. This is exactly the
  kind of local type/syntax failure the high-level loop should repair and retry
  without human intervention.
- A timing-based concurrency assertion was flaky after the run. Tests that
  validate orchestration behavior should prefer direct concurrency observation
  over wall-clock thresholds.
- Accepted reruns after manual commits can produce release summaries with
  `merged: true` and `commit_hash: null` because the diff was already present
  on the integration branch. This edge case is acceptable for dependency
  tracking but should be represented more clearly in release review output.

## Release learning: over-broad docs task

- The `governor-service-docs-and-state-notes` task showed that repo-state
  documentation can overreach if it starts describing the N-epic governor as
  already implemented.
- State notes should only describe verified seams and keep the multi-epic
  governor explicitly marked as planned until the code supports it.
- Typed supervisor decision records should be described as implemented while
  the multi-epic governor loop stays explicitly planned.
- The useful learning from the docs task is to preserve the distinction
  between implemented `GovernorLoop`, `StateStore`, and `RepairPolicy` seams
  and the still-planned multi-epic loop.

## Release learning: missing runtime supervisor

- The `governor-service-boundaries` dogfood run showed that the deterministic
  kernel is doing useful work: it caught stale environment state, schema-invalid
  generated contracts, unsafe allowed-file overlap, long-running worker
  ambiguity, and a documentation task that exceeded the changed-file budget.
- Those findings should not stop a human-out-of-the-loop system. They should
  become inputs to a runtime supervisor agent that observes structured events,
  release summaries, evidence bundles, raw logs, budgets, and tuning signals.
- The supervisor should own bounded repair actions:
  - repair environment or console-script drift;
  - normalize semantically useful but invalid planner contracts;
  - split over-budget contracts;
  - narrow allowed-file overlap without weakening scheduler safety;
  - inspect long-running workers through raw logs and heartbeats;
  - resume from previously accepted tasks instead of rerunning them;
  - update repo-state with learnings after accepted or failed releases.
- Deterministic invariants should remain hard gates. The supervisor may propose
  or apply bounded repairs, but repaired artifacts must still pass admission,
  verification, review, and finalization policy.

## Architecture learning: reduce heuristic code with supervisor tools

- Several modules contain procedural heuristics that exist because no high-level
  supervisor currently owns judgment-heavy recovery:
  - `backlog.py` has deterministic roadmap extraction and scoring that should
    become fallback/test scaffolding once the governor agent is reliable.
  - `planning.py` has generated-contract wording heuristics that should become
    contract-normalization repair actions followed by deterministic admission.
  - `release.py` mixes scheduling, overlap response, human-log formatting,
    release summaries, metrics, budget handling, continuation, and finalization;
    overlap and needs-revision recovery should move to supervisor actions.
  - `failure_diagnosis.py` has brittle log-pattern classification that should
    become evidence packaging plus supervisor-backed diagnosis.
  - `budget.py` should keep numeric ledgers and hard budget checks, but tuning
    recommendations and task-resizing plans should be supervisor decisions.
  - `doctor.py` should keep deterministic diagnostics, but environment repair
    should be a bounded supervisor action.
- This is a maintenance reduction strategy, not a safety relaxation. Remove
  procedural judgment only when the replacement is a typed supervisor action
  that reruns deterministic gates.

## Architectural review: consolidation needed

- `release.py`, `orchestrator.py`, `backlog.py`, `cli.py`, and `models.py` are
  carrying too many responsibilities for the intended multi-epic governor.
- Recent dogfood runs showed two concrete places where deterministic code is
  still doing too much judgment. First, generated contracts for
  `persistent-governor-memory` were blocked before execution because several
  tasks touched `src/agentic_devloop/release.py`; in autonomous mode the
  supervisor should receive an overlap-risk report and choose serialization,
  stacking, re-slicing, or stopping for a true exclusive-path violation.
  Second, release-local feature review can produce multiple useful repair
  passes, but it also risks review churn: new adjacent findings appear after
  previous findings are fixed. The supervisor should classify findings as
  hard blockers, soft findings, duplicates, false positives, scope expansion,
  or backlog follow-ups and stop extending the loop when findings are no longer
  release-blocking.
- Code-reduction goal: do not keep adding Python heuristics for these choices.
  Add typed supervisor decision records instead, then delete or shrink the
  procedural judgment paths after hard validators can consume those decisions.
- The service boundaries now split out the single-epic governor path:
  - `GovernorLoop` for one-epic execution, stopping criteria, and repo-state
    refresh.
  - `StateStore` for backlog state, active releases, completed/blocked epics,
    run summaries, and known learnings.
  - `RepairPolicy` for planner schema repair, verification-environment repair,
    flaky-test retry, narrow merge-conflict repair, and final escalation.
- The remaining architecture increment is the product-facing multi-epic
  governor loop; it should not be recorded as implemented yet.
- `ReleaseScheduler`, `ReleaseReporter`, `ReleaseFinalizer`, and
  `ReleaseMetrics` from `release.py`.
- Task execution, evidence/review, finalization, and repair seams from
  `orchestrator.py`.
- This is not cosmetic refactoring. Without these seams, the high-level
  autonomous loop will continue to accumulate special cases in modules that
  already mix policy, IO, subprocesses, Git state, logs, and artifacts.

## Dogfood learning: multi-epic governor hardening cycle

- The `multi-epic-run-governor-hardening` cycle demonstrated a meaningful
  closed loop: `auto_develop` selected/planned the epic, executed seven
  contracts, ran a release-local semantic review, generated bounded repair
  contracts, accepted two repair waves, and left a coherent feature branch.
- The cycle also showed that "all tasks accepted" is not enough. Final
  integration review still escalated because the reviewer lacked full diff
  context and durable integration-branch verification evidence. A fully
  autonomous governor needs a dedicated integration-review evidence handoff
  that packages complete branch context, reruns final verification on the
  feature branch, records reviewer limitations, and classifies remaining
  findings as blockers, accepted risks, or backlog follow-ups.
- Planner-output normalization remains too reactive. This cycle required
  repair for implementation-requirement fields, wrapper-level dependencies,
  JSON tail drift, and descriptive task-type aliases. These are bounded,
  meaning-preserving repairs and should move behind a typed supervisor
  normalization action rather than growing parser-specific special cases.
- Repair waves are useful but need convergence economics. The first two waves
  fixed real blocker findings; the third review escalated on evidence/context
  limitations. The governor should limit repeated review churn by requiring
  each wave to classify new findings as true blockers, duplicates, accepted
  risks, scope expansions, or backlog follow-ups.
- State-refresh collection is now part of governor execution and therefore
  must be treated as a failure surface. Failures should write explicit
  evidence artifacts with phase, paths, error type, message, and partial
  artifacts before stopping; silent continuation or partial state is not
  acceptable for unattended multi-epic operation.
