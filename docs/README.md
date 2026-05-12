# Documentation

This directory contains user, maintainer, and design documentation for `agentic-devloop`.

## Start Here

- [User Guide](USER_GUIDE.md): step-by-step instructions for using `auto_develop` on an existing development project.
- [Development Guide](DEVELOPMENT.md): setup, test, smoke-test, and documentation practices for contributors.
- [Design Documentation](design/README.md): architecture, technical specification, roadmap, and design risks.

## Project Summary

`agentic-devloop` is an external orchestration layer around coding agents. The orchestrator owns policy, task boundaries, state transitions, budgets, verification, evidence, and explicitly requested Git finalization. Coding agents own implementation inside narrow task contracts.

The project prioritizes:

1. Fast, bounded, pragmatic development loops.
2. Autonomous execution inside explicit contracts.
3. Deterministic verification before trust.
4. Auditable evidence for every accepted change.
5. Safe Git integration through isolated worktrees and feature branches.
6. Scientific conservatism for validation, benchmarks, fixtures, and tolerances.

## Current Implementation

Implemented capabilities include:

- project config and task contract schema validation;
- isolated Git worktree creation;
- Codex CLI executor wrapper with bounded attempts and fallback models;
- `doctor` preflight diagnostics for repo cleanliness, stale worktrees, release-branch collisions, and routing warnings;
- deterministic verification command execution;
- evidence bundle collection;
- deterministic review and persisted decisions;
- repeated-failure diagnosis evidence (`executor_attempts.json` and `failure_diagnosis.yaml`) with deterministic classification by default and a replaceable backend seam for stronger review;
- role-based model routing;
- repo-state context injection;
- `run-task`, `run-release`, `plan-release`, `run-objective`, `status`, and `cleanup` commands;
- release-level feature branch integration through `feature/<release>`;
- dynamic release DAG scheduling from `depends_on` and file-overlap analysis;
- activity-oriented `release.log`, raw `release.raw.log`, `release_metrics.json`, `release_budget.json`, and `release_tuning.md`;
- default cleanup of merged task worktrees and branches;
- dry-run-first manual cleanup;
- one bounded conflict-repair attempt for contract-contained rebase conflicts.

Important remaining gaps:

- stronger model-based repeated-failure diagnosis backend;
- richer semantic merge-conflict repair;
- automated pull-request creation;
- remote execution adapters, including Balfrin/SLURM artifact collection;
- stronger runtime adapters for local/open model roles.

## Documentation Ownership

- Operational instructions belong in [USER_GUIDE.md](USER_GUIDE.md).
- Contributor workflow belongs in [DEVELOPMENT.md](DEVELOPMENT.md).
- Architecture and roadmap decisions belong under [design/](design/).
- Root [README.md](../README.md) should stay short and link into this directory.
- Agent working rules belong in root [AGENTS.md](../AGENTS.md).
