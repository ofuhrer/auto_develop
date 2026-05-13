# Agent Instructions

These instructions apply to the entire repository.

## Project Purpose

This repository contains `agentic-devloop`, exposed as the `agent-loop` CLI. It is a local orchestration tool for bounded autonomous development tasks. The core workflow is:

1. Load project configs, objectives, and task contracts.
2. Create isolated Git worktrees and task branches.
3. Run bounded coding agents.
4. Run deterministic verification.
5. Collect evidence.
6. Review and optionally finalize accepted work into a feature branch or base branch.

Keep changes aligned with that purpose. Prefer pragmatic, testable orchestration increments over broad platform abstractions.

## Repository Layout

- `src/agentic_devloop/`: Python package and CLI implementation.
- `tests/`: pytest suite.
- `configs/`: local project configs used for smoke tests and examples.
- `contracts/`: generated/current task contracts when needed; historical contracts are not kept after their releases land.
- `objectives/`: release objective examples.
- `repo_state/`: durable state for `auto_develop` self-development and examples. External target projects should keep their durable state in the target repo or a dedicated control repo.
- `docs/README.md`: documentation index and current implementation summary.
- `docs/USER_GUIDE.md`: user-facing operational guide.
- `docs/DEVELOPMENT.md`: maintainer setup, testing, smoke-test, and documentation workflow.
- `docs/design/`: architecture, roadmap, technical specification, and critical assessment.
- `runs/`: generated run evidence. Treat as runtime output, not source.

## Development Commands

Install locally:

```bash
uv venv
uv pip install -e ".[dev]"
```

If the editable console script cannot import `agentic_devloop`, use the module path:

```bash
PYTHONPATH=src .venv/bin/python -m agentic_devloop --help
```

Run the full test suite:

```bash
.venv/bin/python -m pytest
```

Run focused tests:

```bash
.venv/bin/python -m pytest tests/test_release.py
```

Run syntax compilation:

```bash
.venv/bin/python -m compileall src
```

Check patch whitespace before finalizing:

```bash
git diff --check
```

## Coding Conventions

- Use Python 3.11-compatible syntax.
- Keep code typed where practical, following the existing dataclass and Pydantic model style.
- Keep modules small and purpose-specific.
- Prefer explicit error messages over silent fallbacks.
- Use deterministic behavior for hard safety invariants, evidence collection, Git operations, and verification execution. Treat judgment-heavy choices as agent-governed soft decisions when the architecture provides a typed evidence path.
- Keep CLI output machine-readable where existing commands already return JSON.
- Avoid broad exception swallowing unless the resulting evidence clearly records the failure.
- Keep comments rare and focused on non-obvious orchestration behavior.

## CLI Behavior Expectations

Preserve these behavior contracts unless intentionally changing them with tests and documentation:

- `run-task` executes one contract in one isolated worktree.
- `run-release` owns `feature/<release>` by default and merges accepted task branches into that integration branch, not directly into `main`.
- `run-release --execution-mode parallel` must respect explicit `depends_on` edges and current inferred file-overlap dependencies. The target design is governor-owned DAG selection: overlap should become a risk signal and hard rejection should be reserved for configured unsafe paths, generated artifacts, lockfiles, migrations, forbidden paths, or out-of-scope files.
- `run-release` must fail fast on stale worktrees or selected stale task branches unless the user explicitly cleans them up.
- `cleanup` must dry-run by default and only remove artifacts with `--force`.
- `cleanup --include-integration-branch` must be explicit before deleting `feature/<release>`.
- Evidence paths, release summaries, and review artifacts should remain inspectable after runs.

## Git And Artifact Safety

- Do not commit generated runtime evidence from `runs/` unless the user explicitly requests it.
- Do not add new target-specific durable state for external repositories to the `auto_develop` source repo unless it is explicitly meant as a tracked example. Prefer the target repo's `.auto_develop/` control directory or a dedicated control repo.
- Do not commit virtual environments, build artifacts, caches, or local worktrees.
- Do not delete preserved worktrees or branches unless the user explicitly asks or a cleanup command is being implemented/tested in a temp repo.
- Do not use destructive Git commands such as `git reset --hard` or `git checkout -- <file>` unless the user explicitly requests them.
- Before running release smoke tests, check for stale worktrees and branches.
- Keep `main` stable; prefer feature branches and reviewable diffs for large changes.

## Testing Expectations

For code changes, run at least:

```bash
.venv/bin/python -m pytest
git diff --check
```

When testing worktree execution, remember that isolated worktrees usually do not contain `.venv`. Verification commands should use the configured shared runtime or an absolute Python path from the main checkout while pointing imports/source paths at the worktree. Do not add new contracts that assume `.venv/bin/python` exists inside each task worktree.

For CLI changes, also run a help smoke test:

```bash
PYTHONPATH=src .venv/bin/python -m agentic_devloop --help
PYTHONPATH=src .venv/bin/python -m agentic_devloop run-release --help
```

For release orchestration changes, add or update focused tests in:

- `tests/test_release.py`
- `tests/test_orchestrator.py`
- `tests/test_cli.py`
- `tests/test_cleanup.py`
- `tests/test_planning.py`

Use temporary repositories in tests. Do not rely on the developer's local Git state.

## Documentation Expectations

Update documentation when behavior changes:

- User workflow changes: update `docs/USER_GUIDE.md`.
- Contributor workflow changes: update `docs/DEVELOPMENT.md`.
- CLI examples or quick-start behavior: update root `README.md`.
- Architecture or roadmap changes: update files under `docs/design/`.
- Config, objective, or contract schema changes: update examples under `configs/`, `objectives/`, or `contracts/` when useful.

Docs should distinguish implemented behavior from planned behavior. Avoid describing aspirational capabilities as if they already work.

## Design Priorities

When making tradeoffs, prioritize:

1. Fast, bounded, pragmatic development loops.
2. Autonomous execution within explicit contracts.
3. Deterministic verification and auditable evidence.
4. Safe Git integration using isolated worktrees and feature branches.
5. Clear stop conditions only after major development steps or unrecoverable safety issues.
6. Agent-readable docs, contracts, and logs.

Avoid:

- unbounded agent prompts;
- hidden global state;
- broad file permissions in task contracts;
- direct uncontrolled writes to `main`;
- deleting debug artifacts without an explicit cleanup path;
- weakening verification to make tests pass.

## Before Finishing A Task

Check:

```bash
git status --short --branch
git diff --check
.venv/bin/python -m pytest
```

Then report:

- files changed;
- tests run;
- any commands that could not be run;
- whether changes are committed or uncommitted.
