# Known Failures

## ad-0001

- Earlier runs failed when the configured Codex model hit quota.
- Earlier verification used `.venv/bin/python` relative to the task worktree; self-test contracts now use the main worktree virtual environment by absolute path.

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
- The next gap is turning this into a persistent multi-epic autonomous loop:
  backlog state, objective/contract generation, release execution, autonomous
  repair/retry, and repo-state updates after each epic.

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
- The useful learning from the docs task is to preserve the distinction
  between implemented `GovernorLoop`, `StateStore`, and `RepairPolicy` seams
  and the still-planned multi-epic loop.

## Architectural review: consolidation needed

- `release.py`, `orchestrator.py`, `backlog.py`, `cli.py`, and `models.py` are
  carrying too many responsibilities for the intended multi-epic governor.
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
