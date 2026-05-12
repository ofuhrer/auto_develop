# Architecture Summary

`agentic-devloop` is an autonomous-first local Python CLI that orchestrates agentic software development in Git worktrees.

Current flow:

1. The roadmap governor reads repository documentation, roadmap, repo-state memory, run artifacts, metrics, and the configured repository goal.
2. The governor selects the next highest-reward epic and emits a validated `BacklogPlan`.
3. The selected epic is written as a release objective.
4. The planner decomposes the objective into bounded task contracts.
5. Release execution creates isolated task worktrees and branches.
6. Worker agents implement inside task contracts.
7. Deterministic verification and review gate acceptance.
8. Accepted work is finalized according to configured autonomous finalization policy.
9. The governor updates roadmap/backlog/repo-state memory from outcomes and evidence before the next cycle.

The orchestrator owns policy, state, budgets, verification, evidence, roadmap governance, and finalization. Worker agents own implementation inside narrow task contracts. Humans provide goals and hard safety boundaries rather than routine approvals.
