# Documentation

This directory contains user, maintainer, and design documentation for `agentic-devloop`.

## Start Here

- [User Guide](USER_GUIDE.md): step-by-step instructions for using `auto_develop` on an existing development project.
- [Development Guide](DEVELOPMENT.md): setup, test, smoke-test, and documentation practices for contributors.
- [Design Documentation](design/README.md): architecture, technical specification, roadmap, and design risks.

## Project Summary

`agentic-devloop` is an autonomous-first orchestration layer around coding agents. The current governor path is split into a one-epic `GovernorLoop` service, a typed `StateStore` persistence seam, and a `RepairPolicy` decision seam. Those seams own roadmap/backlog analysis for the current epic, state persistence, and bounded repair decisions. The next architectural priority is a runtime supervisor that observes release events, diagnoses recoverable failures, applies bounded repair actions, and resumes execution without routine human intervention. The broader multi-epic governor loop and fully automated state refresh remain planned. The orchestrator owns policy, task boundaries, state transitions, budgets, verification, evidence, and configured Git finalization. Coding agents own implementation inside narrow task contracts.

The project prioritizes:

1. Fast, bounded, pragmatic development loops.
2. Autonomous-first planning and execution inside explicit contracts.
3. Deterministic verification before trust.
4. Auditable evidence for every accepted change.
5. Safe Git integration through isolated worktrees and feature branches.
6. Continuous roadmap/backlog refresh from artifacts, validation evidence, metrics, and domain learnings.
7. Autonomous runtime supervision for contract-contained failures.
8. Domain conservatism for validation, benchmarks, fixtures, and tolerances.

## Current Implementation

Implemented capabilities include:

- project config and task contract schema validation;
- isolated Git worktree creation;
- Codex CLI executor wrapper with bounded attempts and fallback models;
- `doctor` preflight diagnostics for repo cleanliness, stale worktrees, release-branch collisions, and routing warnings;
- one-epic `GovernorLoop` service boundaries for selected-epic execution and follow-up state refresh inputs;
- typed `StateStore` persistence over repo-state files and run summaries;
- `RepairPolicy` decision seams for classifying failures into retry or stop decisions;
- deterministic verification command execution;
- evidence bundle collection;
- deterministic review and persisted decisions;
- repeated-failure diagnosis evidence (`executor_attempts.json` and `failure_diagnosis.yaml`) with deterministic classification by default and a replaceable backend seam for stronger review;
- role-based model routing;
- repo-state context injection;
- `run-task`, `run-release`, `plan-backlog`, `plan-release`, `run-objective`, `status`, and `cleanup` commands;
- `run-backlog` for chaining backlog planning into objective and release execution;
- release-level feature branch integration through `feature/<release>`;
- dynamic release DAG scheduling from `depends_on` and file-overlap analysis;
- human-cockpit `release.log`, raw audit `release.raw.log`, `release_metrics.json`, `release_budget.json`, and `release_tuning.md`;
- default cleanup of merged task worktrees and branches;
- dry-run-first manual cleanup;
- one bounded conflict-repair attempt for contract-contained rebase conflicts.

Important remaining gaps:

- multi-epic governor looping beyond the current one-epic service boundary;
- runtime supervisor loop that turns planner/schema, environment, budget, overlap, long-running-worker, and needs-revision events into bounded repair actions;
- reduction of deterministic heuristic code once supervisor-backed decisions are available; candidate areas include backlog scoring, contract-normalization heuristics, failure classification, budget-tuning prose, overlap recovery policy, and cockpit-summary filtering;
- fully automated state refresh across repeated epic cycles;
- stronger model-based repeated-failure diagnosis backend;
- richer semantic merge-conflict repair and broader repair strategies;
- automated pull-request creation;
- repository-instruction-driven remote execution evidence, if a target project requires it;
- stronger runtime adapters for local/open model roles.

## Documentation Ownership

- Operational instructions belong in [USER_GUIDE.md](USER_GUIDE.md).
- Contributor workflow belongs in [DEVELOPMENT.md](DEVELOPMENT.md).
- Architecture and roadmap decisions belong under [design/](design/).
- Root [README.md](../README.md) should stay short and link into this directory.
- Agent working rules belong in root [AGENTS.md](../AGENTS.md).
