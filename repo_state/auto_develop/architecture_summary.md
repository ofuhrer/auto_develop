# Architecture Summary

`agentic-devloop` is an autonomous-first local Python CLI that orchestrates agentic software development in Git worktrees. The runtime-supervisor repair/resume seam is now implemented above the deterministic release kernel so recoverable release failures can be diagnosed, repaired, and resumed without human intervention. The broader N-epic governor loop remains planned until it is actually implemented.

Current flow:

1. The roadmap governor reads repository documentation, roadmap, repo-state memory, run artifacts, metrics, and the configured repository goal.
2. The governor selects the next highest-reward epic and emits a validated `BacklogPlan`.
3. `run-backlog` can select one epic, write or reuse its release objective, plan contracts, and execute the resulting release.
4. The planner decomposes the objective into bounded task contracts.
5. Release execution creates isolated task worktrees and branches.
6. Worker agents implement inside task contracts.
7. Deterministic verification and review gate acceptance.
8. Accepted work is finalized according to configured autonomous finalization policy.
9. A runtime supervisor diagnoses recoverable failures from structured events, evidence, raw logs, budgets, tuning signals, and backlog-state references.
10. The supervisor applies bounded repair actions such as environment repair, planner-contract normalization, task splitting or scope narrowing, release resume, long-running worker inspection, model escalation, and repo-state update proposals.
11. The release path records structured stop evidence and the broader governor/backlog refresh loop remains planned until the N-epic flow is implemented.

The orchestrator owns policy, state, budgets, verification, evidence, roadmap governance, and finalization. Worker agents own implementation inside narrow task contracts. Humans provide goals and hard safety boundaries rather than routine approvals.

Implemented seams:

1. `GovernorLoop` now coordinates one selected epic at a time.
2. `StateStore` persists active, completed, and blocked epic state, plus recent run summaries.
3. `RepairPolicy` classifies retryable versus stop conditions for contract-contained failures, verification drift, missing credentials, and unsafe policy expansion.
4. These seams support the single-epic governor flow and the implemented runtime-supervisor repair/resume loop; the product-facing N-epic loop is still planned.

Code-reduction direction:

1. Keep deterministic code for invariants, evidence, Git safety, verification,
   and budget counters.
2. Move judgment-heavy heuristics into supervisor-backed decisions: backlog
   scoring, contract normalization, failure diagnosis, overlap recovery,
   budget tuning, long-running-worker interpretation, and repo-state updates.
3. Retain old deterministic heuristics only as test scaffolding or fallback
   behavior once typed supervisor actions exist.

Target flow:

1. A freshly cloned `auto_develop` checkout and target repository are onboarded with one or two prompts plus repository policy/configuration.
2. The operator requests a number of epics to implement.
3. The governor loops over the next highest-value epics.
4. The runtime supervisor repairs and retries contract-contained subsystem failures without routine human gates.
5. The loop stops only for major problems: exhausted autonomous repair, missing credentials, unsafe policy expansion, destructive operations not delegated, budget limits, no actionable work, or completion of the requested epic count.
