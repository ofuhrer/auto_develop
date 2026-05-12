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
4. Run one contract with `agent-loop run-task`, or run a release queue with `agent-loop run-release`.
5. Monitor `runs/<release-run-id>/release.log`.
6. Review evidence under `runs/`, including `failure_diagnosis.yaml` and `executor_attempts.json` for failed tasks.
7. Push the feature branch or merge to `main` only when the project policy allows it.

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
