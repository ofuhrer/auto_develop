# Agentic Devloop

`agentic-devloop` is a local CLI orchestrator for bounded autonomous development tasks. It creates isolated Git worktrees, runs coding agents inside explicit task contracts, verifies results deterministically, collects evidence, and optionally finalizes accepted work through Git.

The CLI entry point is:

```bash
agent-loop
```

## Quick Start

Install locally:

```bash
uv venv
uv pip install -e ".[dev]"
```

Check the CLI:

```bash
.venv/bin/agent-loop --help
```

If the editable console script cannot import the package, use:

```bash
PYTHONPATH=src .venv/bin/python -m agentic_devloop --help
```

Run tests:

```bash
.venv/bin/python -m pytest
```

## Typical Workflow

1. Configure a target repository in `configs/<project>.yaml`.
2. Write or generate an objective in `objectives/<release>.yaml`.
3. Write or generate bounded task contracts in `contracts/`.
4. Run `agent-loop doctor` for preflight diagnostics, then run one contract with `agent-loop run-task` or a release queue with `agent-loop run-release`.
5. Monitor `runs/<release-run-id>/release.log` while the release is running and inspect `release_state.json` or task `run_state.json` when a run is interrupted.
6. Review evidence under `runs/`, including `release_metrics.json`, `release_budget.json`, `release_tuning.md`, `failure_diagnosis.yaml`, `validation_review.yaml`, and `executor_attempts.json` for failed tasks.
7. Use the budget ledger and tuning report to adjust `model_routing` or task size before the next run.
8. Use `agent-loop cleanup --force` to recover stale worktrees, branches, or stale merge locks before rerunning a release.
9. Push the feature branch or merge to `main` only when the project policy allows it.

Example:

```bash
agent-loop run-release \
  --project auto_develop \
  --release sprint-0 \
  --merge-on-accept \
  --release-finalize push-feature
```

## Documentation

- [Documentation Index](docs/README.md)
- [User Guide](docs/USER_GUIDE.md)
- [Development Guide](docs/DEVELOPMENT.md)
- [Architecture](docs/design/ARCHITECTURE.md)
- [Roadmap and Backlog](docs/design/ROADMAP_AND_BACKLOG.md)
- [Technical Specification](docs/design/TECHNICAL_SPECIFICATION.md)

Agent-specific repository instructions are in [AGENTS.md](AGENTS.md).
