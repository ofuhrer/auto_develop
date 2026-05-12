# Development Guide

This guide is for contributors changing `agentic-devloop` itself.

## Setup

From the repository root:

```bash
uv venv
uv pip install -e ".[dev]"
```

If the editable console script cannot import `agentic_devloop`, use:

```bash
PYTHONPATH=src .venv/bin/python -m agentic_devloop --help
```

## Common Commands

Run the full test suite:

```bash
.venv/bin/python -m pytest
```

Run focused tests:

```bash
.venv/bin/python -m pytest tests/test_release.py
```

Compile source files:

```bash
.venv/bin/python -m compileall src
```

Check patch whitespace:

```bash
git diff --check
```

Inspect recent runs:

```bash
agent-loop status --limit 5
```

## CLI Smoke Tests

Use the installed command when available:

```bash
.venv/bin/agent-loop --help
.venv/bin/agent-loop --version
.venv/bin/agent-loop config --project auto_develop --validate-repo
```

Use the module fallback when the editable install is not active:

```bash
PYTHONPATH=src .venv/bin/python -m agentic_devloop --help
PYTHONPATH=src .venv/bin/python -m agentic_devloop run-release --help
PYTHONPATH=src .venv/bin/python -m agentic_devloop cleanup --help
```

## Release-Orchestration Smoke Test

Before running against this repository, make sure the main checkout is clean:

```bash
git status --short --branch
agent-loop cleanup --project auto_develop --release smoke-test
```

Then run a bounded contract:

```bash
agent-loop run-release \
  --project auto_develop \
  --release smoke-test \
  --contract contracts/ad-0001.yaml
```

For merge/finalization testing, use an explicit feature-branch flow:

```bash
agent-loop run-release \
  --project auto_develop \
  --release smoke-test \
  --contract contracts/ad-0001.yaml \
  --merge-on-accept \
  --release-finalize push-feature
```

Review `runs/<release-run-id>/release.log`, `release.raw.log`, `release_summary.json`, `release_metrics.json`, and `release_review.md`.

## Documentation Rules

Update documentation when behavior changes:

- User workflow changes: update [USER_GUIDE.md](USER_GUIDE.md).
- Contributor workflow changes: update this file.
- CLI quick-start changes: update root [README.md](../README.md).
- Architecture, roadmap, or design-policy changes: update [design/](design/).
- Agent working rules: update root [AGENTS.md](../AGENTS.md).

Documentation should distinguish implemented behavior from planned behavior. Do not describe aspirational capabilities as operational.

## Safety Rules

- Do not commit generated runtime evidence from `runs/` unless explicitly requested.
- Do not commit virtual environments, build artifacts, caches, or local worktrees.
- Use temp repositories in tests instead of relying on local Git state.
- Do not weaken verification to make tests pass.
- Do not use destructive Git commands unless explicitly requested.

## Before Finishing

For most code changes, run:

```bash
git diff --check
.venv/bin/python -m pytest
```

For CLI changes, also run:

```bash
PYTHONPATH=src .venv/bin/python -m agentic_devloop --help
PYTHONPATH=src .venv/bin/python -m agentic_devloop run-release --help
```
