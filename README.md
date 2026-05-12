# Agentic Devloop

`agentic-devloop` is an autonomous-first local CLI orchestrator for agentic software development. A governor agent reads repository documentation, roadmap, artifacts, and goals to choose the next high-reward epic; worker agents implement bounded contracts in isolated worktrees; deterministic tools enforce hard invariants and run verification; and a runtime supervisor should diagnose, repair, and resume contract-contained failures without routine human intervention. Soft findings such as modest budget overages, normal source-file overlap, retry strategy, model escalation, and environment repair are intended to be decided by the governor or supervisor agent with auditable rationale rather than by brittle deterministic rejection. Accepted soft findings are written as explicit decision artifacts, including `soft_gate_decision.json` for task evidence bundles and `soft_gate_decisions.json` for release-level budget exceptions; those records carry the finding, severity, risk, evidence paths, recommended actions, decision, rationale, fallback plan, and validators to rerun. Hard gates remain non-bypassable for forbidden paths, generated artifacts, missing required evidence, unsafe finalization, credentials or network policy violations, destructive operations, and unrepaired verification failures. The broader multi-epic governor loop remains planned. The orchestrator records evidence, updates state, and finalizes accepted work according to configured policy.

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
2. Run `agent-loop plan-backlog --mode strong-model --execute-planner` so the governor agent selects the next epic from docs, roadmap, artifacts, and the repository goal.
3. Run `agent-loop run-objective` so an existing objective is decomposed into contracts and executed by worker agents. If you want the combined flow, use `agent-loop run-backlog` to chain backlog selection, objective creation or reuse, and release execution in one command.
4. Run `agent-loop doctor` for preflight diagnostics when launching governed release execution.
5. Monitor `runs/<release-run-id>/release.log` while the release is running.
6. Review evidence under `runs/`, including `release_metrics.json`, `release_budget.json`, `release_tuning.md`, `failure_diagnosis.yaml`, and `executor_attempts.json` for failed tasks.
7. Let the governor and runtime supervisor use budget, tuning, validation, and artifact evidence to repair recoverable failures and update roadmap/backlog state before the next run.
8. Push the feature branch or merge to `main` when the configured project policy allows autonomous finalization.

For external target repositories, keep durable target development memory with the target, not with the `auto_develop` implementation checkout. A good default is `<target>/.auto_develop/repo_state`, `<target>/.auto_develop/objectives`, and `<target>/.auto_develop/contracts` for tracked state, with raw runs ignored or externally archived.

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
