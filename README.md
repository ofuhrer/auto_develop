# Agentic Devloop

Agentic Devloop is a local CLI orchestrator for bounded AI development tasks.

The first implementation target is a pragmatic loop that can:

1. Load project and task definitions.
2. Create isolated Git worktrees.
3. Run a bounded coding agent.
4. Run deterministic verification.
5. Collect evidence.
6. Produce an accept, reject, or escalate decision.

See [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) and [docs/design](docs/design) for the design documents.

## Development

Install locally:

```bash
uv venv
uv pip install -e ".[dev]"
```

Run tests:

```bash
pytest
```

Check the CLI:

```bash
agent-loop --help
agent-loop --version
```
