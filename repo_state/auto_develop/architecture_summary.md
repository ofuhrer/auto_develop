# Architecture Summary

`agentic-devloop` is an autonomous-first local Python CLI that orchestrates agentic software development in Git worktrees. The runtime-supervisor repair/resume seam is now implemented above the deterministic release kernel so recoverable release failures can be diagnosed, repaired, and resumed without human intervention. Planner-output normalization, one-epic governor logging, deterministic state-review snapshot capture, persistent governor memory seams, typed supervisor decision records, and the release-local feature-review/repair loop are implemented. The broader N-epic governor loop and full agent-driven pre-epic state-review decisioning remain planned.

Current flow:

1. The roadmap governor reads repository documentation, roadmap, repo-state memory, run artifacts, metrics, and the configured repository goal.
2. The governor selects the next highest-reward epic and emits a validated `BacklogPlan`.
3. `run-backlog` can select one epic, write or reuse its release objective, plan contracts, and execute the resulting release.
4. The planner decomposes the objective into bounded task contracts.
5. Release execution creates isolated task worktrees and branches.
6. Worker agents implement inside task contracts.
7. Deterministic verification gates hard invariants and emits structured findings.
8. Accepted work is finalized according to configured autonomous finalization policy.
9. A runtime supervisor diagnoses recoverable failures from structured events, evidence, raw logs, budgets, tuning signals, and backlog-state references.
10. The supervisor applies bounded repair actions such as environment repair, planner-contract normalization, task splitting or scope narrowing, release resume, long-running worker inspection, model escalation, and repo-state update proposals.
11. A reviewer/supervisor agent should decide soft findings such as modest budget overage, normal source-file overlap, retry strategy, environment repair, model escalation, and task splitting; deterministic code remains authoritative for hard invariants.
12. The release path records structured stop evidence and the broader governor/backlog refresh loop remains planned until the N-epic flow is implemented.
13. Release planning can persist a deterministic `state_review_snapshot.json` artifact and pass its path via `state_review_snapshot_path` in contract-plan payloads.

Target additions:

1. Before selecting an epic, the state-review governor should expand beyond snapshot capture to full decisioning over live repository state: branch status, dirty state, open feature/agent branches, source layout drift, changed docs, recent release artifacts, release reviews, metrics, tuning reports, unresolved findings, and tracked repo-state memory.
2. The implemented release-local feature-review loop should be composed into the broader governor so every epic receives PR-style semantic review before PR, merge, or autonomous finalization.
3. Reviewer findings already become bounded repair contracts in one release; the target addition is convergence policy and durable memory across repeated epic cycles.
4. The system should finalize only after required reviewer findings are resolved, explicitly accepted with rationale, or stopped by retry budget, hard gates, missing policy/credentials, or configured human escalation.

Prioritized architectural gaps:

1. State-review governor before backlog selection.
2. Supervisor-owned release scheduling from overlap-risk reports.
3. Review-loop convergence policy for repeated reviewer/repair cycles.
4. N-epic governor command once selection, review, memory, and scheduling are reliable.
5. Governor cockpit expansion for full multi-epic visibility.
6. Shared verification runtime and bounded environment repair.
7. Executor liveness supervision.
8. Target artifact ownership and onboarding bootstrap.

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
4. Reclassify exact source overlap and small budget overages as soft findings
   for agent judgment; keep hard stops for forbidden paths, generated artifacts,
   configured exclusive paths, missing evidence, unsafe finalization, and
   unrepaired verification/runtime failures.

Target flow:

1. A freshly cloned `auto_develop` checkout and target repository are onboarded with one or two prompts plus repository policy/configuration.
2. The operator requests a number of epics to implement.
3. The governor refreshes repository state before each selection, then loops over the next highest-value epics.
4. The runtime supervisor repairs and retries contract-contained subsystem failures without routine human gates.
5. An independent reviewer agent reviews each integrated feature branch, and repair agents address findings before PR, merge, or configured autonomous finalization.
6. The loop stops only for major problems: exhausted autonomous repair, unresolved required review findings, missing credentials, unsafe policy expansion, destructive operations not delegated, hard budget or invariant limits, no actionable work, or completion of the requested epic count.
