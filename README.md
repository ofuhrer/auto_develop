# Agentic Devloop

`agentic-devloop` is an autonomous-first local CLI orchestrator for agentic software development. The current implementation ships one-epic backlog planning, deterministic state-review snapshot capture, persistent governor memory seams, typed supervisor decision records, supervisor-owned release scheduling for normal source overlap, and a release-local semantic feature-review loop over integrated feature branches when `model_roles.reviewer` is configured. The architecture is pivoting toward a deterministic kernel plus a high-level governor/supervisor agent: deterministic code owns hard gates, Git/worktrees, verification, evidence, metrics, typed persistence, and finalization mechanics; the supervisor owns judgment-heavy choices such as one-shot versus decomposed execution, reviewer-output normalization, finding adjudication, retry/split/replan decisions, and roadmap updates. `plan-backlog` and `run-backlog` can use a strong-model planner over bounded docs, roadmap, repo-state memory, and goals for one epic at a time, and the code includes a deterministic state-review snapshot artifact (`state_review_snapshot.json`) plus contract-plan wiring for `state_review_snapshot_path`. Normal source-file overlap now produces overlap-risk reporting and a typed `release_scheduling` supervisor decision instead of an unconditional stop, while hard gates remain non-bypassable for forbidden paths, generated artifacts, lockfiles, migrations, configured exclusive paths, forbidden paths, missing required evidence, unsafe finalization, credentials or network policy violations, destructive operations, and unrepaired verification failures. Full autonomous pre-epic state-review decisioning, supervisor-owned execution-strategy selection, model-output normalization before strict validation, and N-epic governor behavior are still planned. Accepted soft findings are written as explicit decision artifacts, including `soft_gate_decision.json`, `soft_gate_decisions.json`, and typed supervisor decision records under `supervisor_decisions/`; those records carry the finding, severity, risk, evidence paths, recommended actions, decision, rationale, fallback plan, and validators to rerun. The orchestrator records evidence, updates state, and finalizes accepted work according to configured policy.

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
