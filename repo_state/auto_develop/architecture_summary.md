# Architecture Summary

`agentic-devloop` is an autonomous-first local Python CLI that orchestrates agentic software development in Git worktrees.

Current flow:

1. The roadmap governor reads repository documentation, roadmap, repo-state memory, run artifacts, metrics, and the configured repository goal.
2. The governor selects the next highest-reward epic and emits a validated `BacklogPlan`.
3. `run-backlog` can select one epic, write or reuse its release objective, plan contracts, and execute the resulting release.
4. The planner decomposes the objective into bounded task contracts.
5. Release execution creates isolated task worktrees and branches.
6. Worker agents implement inside task contracts.
7. Deterministic verification and review gate acceptance.
8. Accepted work is finalized according to configured autonomous finalization policy.
9. The governor updates roadmap/backlog/repo-state memory from outcomes and evidence before the next cycle.

The orchestrator owns policy, state, budgets, verification, evidence, roadmap governance, and finalization. Worker agents own implementation inside narrow task contracts. Humans provide goals and hard safety boundaries rather than routine approvals.

Target flow:

1. A freshly cloned `auto_develop` checkout and target repository are onboarded with one or two prompts plus repository policy/configuration.
2. The operator requests a number of epics to implement.
3. The governor loops over the next highest-value epics, repairing and retrying contract-contained subsystem failures without routine human gates.
4. The loop stops only for major problems: exhausted autonomous repair, missing credentials, unsafe policy expansion, destructive operations not delegated, budget limits, no actionable work, or completion of the requested epic count.
