# Known Failures

## Closed-loop run learning: unsupported primary worker

- The first full release run showed that `gpt-5.3-codex-spark` is not
  supported for the current Codex ChatGPT account.
- Project configs now express the recommended model hierarchy with safe
  fallbacks and `model_catalog` availability diagnostics. `gpt-5.3-codex-spark`
  remains a repair-role candidate with fallback to `gpt-5.4-mini`.

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
