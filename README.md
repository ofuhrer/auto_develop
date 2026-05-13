# Agentic Devloop

`agentic-devloop` is an autonomous-first local CLI orchestrator for agentic software development. The current implementation ships one-epic backlog planning, deterministic state-review snapshot capture, and a release-local semantic feature-review loop over integrated feature branches when `model_roles.reviewer` is configured. That loop writes `feature_review.json` and `feature_review_recheck.json`, converts required findings into bounded repair contracts, reruns verification, and blocks finalization until required findings are resolved. The broader design still includes a state-review governor that reads repository documentation, roadmap, source state, branches, run artifacts, metrics, and goals before choosing the next high-reward epic; worker agents implement bounded contracts in isolated worktrees; repair agents address reviewer findings; deterministic tools enforce hard invariants and run verification; and a runtime supervisor diagnoses, repairs, and resumes contract-contained failures without routine human intervention. `plan-backlog` and `run-backlog` can use a strong-model planner over bounded docs, roadmap, repo-state memory, and goals for one epic at a time, and the code includes a deterministic state-review snapshot artifact (`state_review_snapshot.json`) plus contract-plan wiring for `state_review_snapshot_path`. The feature-review loop is implemented as a single release boundary; full autonomous pre-epic state-review decisioning, persistent governor memory, and N-epic governor behavior are still planned. Soft findings such as modest budget overages, normal source-file overlap, retry strategy, model escalation, and environment repair are intended to be decided by the governor or supervisor agent with auditable rationale rather than by brittle deterministic rejection. Accepted soft findings are written as explicit decision artifacts, including `soft_gate_decision.json` for task evidence bundles and `soft_gate_decisions.json` for release-level budget exceptions; those records carry the finding, severity, risk, evidence paths, recommended actions, decision, rationale, fallback plan, and validators to rerun. Hard gates remain non-bypassable for forbidden paths, generated artifacts, missing required evidence, unsafe finalization, credentials or network policy violations, destructive operations, and unrepaired verification failures. The orchestrator records evidence, updates state, and finalizes accepted work according to configured policy.

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
2. Run `agent-loop plan-backlog --mode strong-model --execute-planner` so the governor agent selects the next epic from docs, roadmap, repo-state memory, and the repository goal.
3. Run `agent-loop run-objective` so an existing objective is decomposed into contracts and executed by worker agents. If you want the combined flow, use `agent-loop run-backlog` to chain backlog selection, objective creation or reuse, and release execution in one command.
4. Run `agent-loop doctor` for preflight diagnostics when launching governed release execution.
5. Monitor `runs/<release-run-id>/release.log` while the release is running.
6. Review evidence under `runs/`, including `release_metrics.json`, `release_budget.json`, `release_tuning.md`, `failure_diagnosis.yaml`, and `executor_attempts.json` for failed tasks.
7. Let the governor and runtime supervisor use budget, tuning, validation, and artifact evidence to repair recoverable failures and update roadmap/backlog state before the next run.
8. Push the feature branch or merge to `main` when the configured project policy allows autonomous finalization.

For external target repositories, keep durable target development memory with the target, not with the `auto_develop` implementation checkout. A good default is `<target>/.auto_develop/repo_state`, `<target>/.auto_develop/objectives`, and `<target>/.auto_develop/contracts` for tracked state, with raw runs ignored or externally archived.

Planned governor behavior includes one parent `governor.log` for watching a full N-epic run, a pre-epic state-review pass that refreshes backlog memory from source/release artifacts before selection, and broader multi-epic orchestration after the reviewer and memory layers are in place. The shipped feature-review pass runs only when `model_roles.reviewer` is configured and remains release-local rather than a full multi-epic governor. Bounded contract normalization is now part of the planning path for useful planner output that fails deterministic admission for repairable issues such as missing required evidence or worktree-local verification commands.

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
