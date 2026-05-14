# Agentic Devloop

`agentic-devloop` is an autonomous-first local CLI orchestrator for agentic software development. The current implementation ships one-epic backlog planning, a repeated-cycle `run-governor --epic-count N` shell, parent `governor.log`/`events.jsonl` artifacts, completed-epic tracking, recent release-summary recording, deterministic state-review snapshot capture, post-cycle state-refresh evidence, persistent governor memory seams, typed supervisor decision records, supervisor-owned release scheduling for normal source overlap, a shared verification-runtime policy for isolated worktrees, a release-local semantic feature-review loop over integrated feature branches when `model_roles.reviewer` is configured, and cost-runtime governance that reads prior `release_metrics.json`/`release_tuning.md` evidence to choose decomposed, one-shot, or review-capped routing for the next release. That shipped review loop now exposes the full evidence trail: reviewer prompt/input, raw stdout/stderr/metadata, normalized reviewer output when bounded normalization applies, recheck output, the final review-continuation decision, final integration verification evidence, generated repair contracts, and backlog follow-up proposal paths. Final review stop hardening adds the supervisor-governed convergence boundary: after bounded repairs end, the integrated branch is rerun through final verification before final adjudication, blocker findings stay blocked, soft and false-positive or verification-only findings may continue only with rationale, duplicate findings are deferred, and scope-expansion or backlog-follow-up findings are deferred into repo-state follow-up. Finalization follows explicit policy selection: `local_merge`, `push_feature`, and `pr_preparation` are implemented policy values, while missing finalization policy or missing credentials produce explicit `missing_policy` and `missing_credentials` stops instead of guessing. The refresh path splits into a pre-selection state review that writes `state_refresh_summary.json` alongside `state_review_snapshot.json`, and a post-cycle refresh that writes `post_cycle_state_refresh.json`; if either refresh fails, it writes `state_refresh_error.json` and stops before the next epic selection step. The refresh outcome compacts durable repo-state fields such as `active_epics`, `reviewed_epics`, `completed_epic_records`, `blocked_epic_records`, `recent_run_summaries`, and finalization outcome references so the next selection step does not rely on raw `runs/` artifacts alone. The architecture is pivoting toward a deterministic kernel plus a high-level governor/supervisor agent: deterministic code owns hard gates, Git/worktrees, verification, evidence, metrics, typed persistence, and finalization mechanics; the supervisor owns judgment-heavy choices such as one-shot versus decomposed execution, reviewer-output normalization, finding adjudication, retry/split/replan decisions, and roadmap updates. `plan-backlog`, `run-backlog`, and `run-governor` can use a strong-model planner over bounded docs, roadmap, repo-state memory, and goals; `run-governor` composes repeated one-epic cycles and stops on non-executable or non-accepted cycles by default. Normal source-file overlap now produces overlap-risk reporting and a typed `release_scheduling` supervisor decision instead of an unconditional stop, while hard gates remain non-bypassable for forbidden paths, generated artifacts, lockfiles, migrations, configured exclusive paths, forbidden paths, missing required evidence, unsafe finalization, credentials or network policy violations, destructive operations, and unrepaired verification failures. Full autonomous pre-epic state-review decisioning, one-shot worker execution, autonomous final-review repair continuation, and autonomous branch cleanup/finalization are still planned. Accepted soft findings are written as explicit decision artifacts, including `soft_gate_decision.json`, `soft_gate_decisions.json`, and typed supervisor decision records under `supervisor_decisions/`; those records carry the finding, severity, risk, evidence paths, recommended actions, decision, rationale, fallback plan, and validators to rerun. The orchestrator records evidence, updates state, and finalizes accepted work according to configured policy.

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
3. Run `agent-loop run-objective` so an existing objective is decomposed into contracts and executed by worker agents. If you want the combined flow, use `agent-loop run-backlog` for one epic or `agent-loop run-governor --epic-count N` for repeated epic cycles.
4. Run `agent-loop doctor` for preflight diagnostics when launching governed release execution.
5. Monitor `runs/<release-run-id>/release.log` while the release is running.
6. Review evidence under `runs/`, including `release_metrics.json`, `release_budget.json`, `release_tuning.md`, `cost_runtime_governance__<release-id>.json`, `failure_diagnosis.yaml`, and `executor_attempts.json` for failed tasks.
7. Let the governor and runtime supervisor use budget, tuning, validation, and artifact evidence to repair recoverable failures, apply cost/runtime routing, and update roadmap/backlog state before the next run.
8. Push the feature branch or merge to `main` when the configured project policy allows autonomous finalization.

For self-development, the shared verification runtime usually lives in the `auto_develop/main` checkout, and worktree verification should point at that runtime instead of assuming a per-worktree `.venv`. For external target repositories, keep durable target development memory with the target, not with the `auto_develop` implementation checkout. A good default is `<target>/.auto_develop/repo_state`, `<target>/.auto_develop/objectives`, and `<target>/.auto_develop/contracts` for tracked state, with raw runs ignored or externally archived.

`run-governor` now creates a parent `governor.log`/`events.jsonl` stream for repeated epic cycles. It reuses the current one-epic planner/objective/release machinery, marks completed epics in repo state, records recent release summaries, and stops on planning-only or non-accepted cycles unless policy allows continuation. The shipped feature-review pass runs only when `model_roles.reviewer` is configured and remains release-local; finalization-policy selection, PR-preparation handoff artifacts, and explicit missing-policy or missing-credential stop states are implemented, while autonomous final-review repair continuation, final branch cleanup, and full pre-epic state-review decisioning remain planned. The cost-runtime governance decision is driven by local release metrics and tuning evidence only; it does not imply billed-cost accounting, provider-token telemetry, or broad context retrieval. Worktree verification should continue to use the configured shared runtime rather than a local `.venv`.

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
