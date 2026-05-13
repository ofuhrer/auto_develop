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

The repository no longer tracks historical smoke-test contracts. Before running
against this repository, create or generate a small contract for the current
epic and make sure the main checkout is clean:

```bash
git status --short --branch
agent-loop cleanup --project auto_develop --release smoke-test
```

Then run the bounded contract you created:

```bash
agent-loop run-release \
  --project auto_develop \
  --release smoke-test \
  --contract contracts/<task-id>.yaml
```

For merge/finalization testing, use an explicit feature-branch flow:

```bash
agent-loop run-release \
  --project auto_develop \
  --release smoke-test \
  --contract contracts/<task-id>.yaml \
  --merge-on-accept \
  --release-finalize push-feature
```

Review `runs/<release-run-id>/release.log`, `release.raw.log`, `release_summary.json`, `release_metrics.json`, `release_budget.json`, `release_tuning.md`, and `release_review.md`. Treat `release.log` as the human cockpit and `release.raw.log` as the complete audit stream. When `model_roles.reviewer` is configured, also inspect `feature_review.json`, `feature_review_recheck.json`, and any bounded repair-contract evidence under the release run; these artifacts are the semantic-review trail and repair-contract record, while `release_review.md` remains the deterministic evidence summary.

Feature review backend notes (implemented today):
- The reviewer executor uses Codex CLI (`executor.type: codex_cli`). `run-release` preflights the reviewer backend and blocks review immediately when `codex` is missing from `PATH` or the configured reviewer executor type is unsupported.
- When the reviewer backend is unsupported, missing, or returns invalid output, `feature_review.json` records an `escalate` decision with a critical finding that includes stable remediation hints (install/configure `codex`, verify `model_roles.reviewer`, or disable semantic review and use deterministic/human review).

Soft-gate exceptions are recorded in the evidence bundle or release root, not hidden in logs. Task-level findings live at `runs/<run-id>/<task-id>/evidence/soft_gate_decision.json`; release-level budget exceptions live at `runs/<run-id>/soft_gate_decisions.json`. Each record includes the finding identifier, severity, risk, recommended actions, evidence paths, decision, rationale, fallback plan, and `validators_rerun`. The rerun list is the reviewer/supervisor checklist for repeating or re-reading the relevant validators before the soft exception is treated as durable. Hard validation still comes first, and the reviewer loop remains bounded to one integrated feature branch per release. Typed supervisor decision records under `runs/<run-id>/**/supervisor_decisions/` are the implemented durable audit trail for the scheduler, repair, and soft-budget choices that support that loop, including `release_scheduling` decisions with `selected_action`, `fallback_plan`, `validators_to_rerun`, and strict staleness inputs. Loading them is strict rather than warning-only.

Supervisor decision artifacts are persisted as deterministic JSON files under `runs/<run-id>/supervisor_decisions/` with filenames `<decision_type>__<decision_id>.json`. Loading these artifacts is strict: schema validation failures and missing referenced evidence paths are hard errors, not warning-only conditions. This typed record trail is implemented; the broader N-epic governor that would consume it across repeated epics remains planned.

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
