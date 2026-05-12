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
uv pip install ".[dev]"
```

For editable development installs in environments that ignore editable `.pth` path entries, use:

```bash
PYTHONPATH=src uv run agent-loop --help
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

Run the local CLI smoke checks:

```bash
PYTHONPATH=src uv run agent-loop --help
PYTHONPATH=src uv run agent-loop --version
PYTHONPATH=src uv run agent-loop config --project rust_rockfall
```

Load a project config:

```bash
agent-loop config --project rust_rockfall
```

Run one bounded task:

```bash
agent-loop run-task \
  --project rust_rockfall \
  --contract contracts/rr-0001.yaml
```

`run-task` creates an isolated worktree, writes an executor prompt, runs the configured executor, runs the contract verification commands, collects an evidence bundle, and writes `decision.yaml`.

Before running against `rust_rockfall`, update [configs/rust_rockfall.yaml](configs/rust_rockfall.yaml) so `repo_path` and `worktree_root` point to real local paths.
