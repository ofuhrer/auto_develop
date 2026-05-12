# Architecture Summary

`agentic-devloop` is a local Python CLI that orchestrates bounded AI coding tasks in Git worktrees.

Current flow:

1. Load project config and task contract.
2. Create an isolated task worktree and branch.
3. Build a contract-first executor prompt.
4. Run Codex CLI.
5. Run deterministic verification commands.
6. Collect evidence.
7. Run deterministic review.
8. Optionally commit, merge, and push accepted work when explicit finalization flags are used.

The orchestrator owns policy, state, budgets, verification, evidence, and finalization. The executor owns only implementation inside the task contract.
