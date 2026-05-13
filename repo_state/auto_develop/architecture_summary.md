# Architecture Summary

`agentic-devloop` is an autonomous-first local Python CLI that orchestrates agentic software development in Git worktrees. The durable architectural direction is now a deterministic kernel plus a high-level governor/supervisor agent. The deterministic kernel owns Git/worktree mechanics, hard safety gates, verification, evidence, metrics, typed artifact persistence, and finalization policy. The governor/supervisor owns judgment-heavy choices: epic selection, one-shot versus decomposed execution, scheduling, planner-output normalization, finding adjudication, repair/retry/split decisions, and roadmap memory updates. Runtime-supervisor repair/resume seams, planner-output normalization, one-epic governor logging, an initial repeated-cycle `run-governor --epic-count N` shell, deterministic state-review snapshot capture, persistent governor memory seams, typed supervisor decision records, supervisor-owned release scheduling, supervisor-owned execution-strategy selection, one-shot execution input materialization, and the release-local feature-review/repair loop are implemented. The `multi-epic-run-governor-hardening` cycle added per-cycle state-refresh summaries, typed governor stop reasons, no-actionable-work detection, finalization/cleanup handoff metadata, feature-review continuation metadata, parent artifact manifests, and state-refresh failure evidence. The remaining product gap is no longer basic repeated-cycle execution; it is final integration-review evidence handoff, autonomous finalization/cleanup, reviewer and supervisor output normalization extensions, and full agent-driven pre-epic state-review decisioning.

Current flow:

1. The roadmap governor reads repository documentation, roadmap, repo-state memory, run artifacts, metrics, and the configured repository goal.
2. The governor selects the next highest-reward epic and emits a validated `BacklogPlan`.
3. `run-backlog` can select one epic, write or reuse its release objective, plan contracts, and execute the resulting release.
4. `run-governor --epic-count N` composes repeated `run-backlog`-style one-epic cycles, writes parent governor logs/events, updates completed-epic state, records recent release summaries, and stops on planning-only or non-accepted cycles by default.
5. The shipped one-epic execution-strategy seam lets the supervisor choose execution strategy before contract generation: one-shot input materialization, sequential contracts, parallel contracts, stacked branches, patch handoff, replanning, or stop. `stop` currently persists selection JSON only; typed blocked-decision persistence remains planned.
6. If decomposition is selected, the planner decomposes the objective into bounded task contracts; if `one_shot` is selected today, planning writes `one_shot_execution_input.json` and stops before worker execution.
7. Release execution creates isolated task worktrees and branches.
8. Worker agents implement inside the selected strategy and task boundaries.
9. Deterministic verification gates hard invariants and emits structured findings.
10. Accepted work is finalized according to configured autonomous finalization policy.
11. A runtime supervisor diagnoses recoverable failures from structured events, evidence, raw logs, budgets, tuning signals, and backlog-state references.
12. The supervisor applies bounded repair actions such as environment repair, planner output normalization, task splitting or scope narrowing, release resume, long-running worker inspection, model escalation, and repo-state update proposals. Reviewer and supervisor output normalization remain planned extensions on the same seam.
13. A reviewer/supervisor agent should decide soft findings such as modest budget overage, normal source-file overlap, retry strategy, environment repair, model escalation, and task splitting; deterministic code remains authoritative for hard invariants.
14. Release planning can persist a deterministic `state_review_snapshot.json` artifact and pass its path via `state_review_snapshot_path` in contract-plan payloads.

Target additions:

1. Before selecting an epic, the state-review governor should expand beyond snapshot capture to full decisioning over live repository state: branch status, dirty state, open feature/agent branches, source layout drift, changed docs, recent release artifacts, release reviews, metrics, tuning reports, unresolved findings, and tracked repo-state memory.
2. Implement the one-shot worker runner that consumes `one_shot_execution_input.json`, runs a bounded high-capability implementation, verifies it, records evidence, and produces a mergeable feature branch.
3. Add typed blocked-decision persistence for `stop` execution-strategy outcomes.
4. Before generating contracts, the supervisor should normalize useful planner, reviewer, and supervisor output into strict typed artifacts with validator-rerun metadata before hard gates decide whether to stop.
5. The implemented release-local feature-review loop should be composed into the broader governor so every epic receives PR-style semantic review before PR, merge, or autonomous finalization.
6. Reviewer findings already become bounded repair contracts in one release; the target addition is output normalization, convergence policy, and durable memory across repeated epic cycles.
7. The system should finalize only after required reviewer findings are resolved, explicitly accepted with rationale, or stopped by retry budget, hard gates, missing policy/credentials, or configured human escalation.

Prioritized architectural gaps:

1. Integration-review evidence handoff: final verification on the feature branch, complete diff/context packaging, reviewer limitation capture, and typed continuation decisions for blocker versus accepted risk versus backlog follow-up.
2. Autonomous finalization and cleanup after accepted integration review: merge or PR, push, branch/worktree cleanup, and durable state-memory update.
3. General supervisor-owned output normalization for planner schema drift before strict typed admission, with reviewer and supervisor extensions still planned.
4. One-shot worker execution from `one_shot_execution_input.json`.
5. Typed blocked-decision persistence for `stop` execution-strategy outcomes.
6. Full state-review governor decisioning before backlog selection.
7. Governor cockpit expansion for full multi-epic visibility.
8. Shared verification runtime, bounded environment repair, executor liveness supervision, target artifact ownership, and onboarding bootstrap.

The orchestrator owns policy, state, budgets, verification, evidence, roadmap governance, and finalization. Worker agents own implementation inside narrow task contracts. Humans provide goals and hard safety boundaries rather than routine approvals.

Implemented seams:

1. `GovernorLoop` now coordinates one selected epic at a time and has an initial repeated-cycle method for multiple one-epic cycles.
2. `StateStore` persists active, completed, and blocked epic state, plus recent run summaries.
3. `RepairPolicy` classifies retryable versus stop conditions for contract-contained failures, verification drift, missing credentials, and unsafe policy expansion.
4. `ExecutionStrategy` now selects the one-epic execution mode before contract generation and writes typed selection evidence plus one-shot execution input when applicable.
5. These seams support the single-epic governor flow and the implemented runtime-supervisor repair/resume loop; the product-facing N-epic loop is still planned.

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
