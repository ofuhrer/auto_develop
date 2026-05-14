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

## Shared Verification Runtime

Worktree verification should use one configured runtime from the main checkout or a shared control location, not a `.venv` inside each task worktree.

For self-development in this repository, a typical command looks like:

```bash
PYTHONPATH=/path/to/auto_develop/worktrees/<task-worktree>/src /path/to/auto_develop/main/.venv/bin/python -m pytest
```

For external target repositories, put the same pattern in that project's `verification_profiles` and point the Python executable at the shared runtime for the target or control repository. Keep the worktree-specific part in `PYTHONPATH` or an equivalent wrapper, not in a per-worktree virtual environment.

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
PYTHONPATH=src .venv/bin/python -m agentic_devloop run-governor --help
PYTHONPATH=src .venv/bin/python -m agentic_devloop cleanup --help
```

Smoke expectations:

- `run-release --help` should expose `--release-finalize` with `none`, `push-feature`, `merge-main`, and `push-main`.
- `run-governor --help` should expose `--epic-count`, `--goal`, `--execute-planner`, and `--release-finalize`.
- `cleanup --help` should expose `--force` and `--include-integration-branch`.
- The module fallback should work even when the editable console script is missing from `PATH`.

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

Review `runs/<release-run-id>/release.log`, `release.raw.log`, `release_summary.json`, `release_metrics.json`, `release_budget.json`, `release_tuning.md`, and `release_review.md`. Treat `release.log` as the human cockpit and `release.raw.log` as the complete audit stream. When `model_roles.reviewer` is configured, also inspect `feature_review_prompt.md`, `feature_review_stdout.log`, `feature_review_stderr.log`, `feature_review_metadata.json`, `feature_review.json`, `feature_review_recheck.json`, `supervisor_decisions/final_review_finding_adjudication__<id>.json`, the output-normalization decision artifact when present, and any bounded repair-contract evidence under the release run; these artifacts are the semantic-review trail and repair-contract record, while `release_review.md` remains the deterministic evidence summary. The final integration verification rerun happens before the final adjudication decision, and accepted or deferred findings should be compacted into repo-state memory rather than mirrored as raw review output. Normalized planner, reviewer, and supervisor output must stay inside the original objective, allowed files, forbidden changes, and stop conditions; when the repair would broaden scope or change intent, the supervisor must refuse rather than paper over the defect.

For final-review stop hardening, verify that blocker findings remain blocked until they are resolved or escalation is forced by hard policy, missing policy/credentials, or retry-budget exhaustion. Soft, false-positive, and verification-only findings should be accepted only with rationale after the final integration verification rerun passes. Duplicate findings should be deferred, and scope-expansion or backlog-follow-up findings should become repo-state follow-up rather than accepted work.

If `release_finalization_policy` is set to `pr_preparation`, expect a `pr_handoff.json` artifact and no remote push or merge. The configured policy is authoritative; `--release-finalize` is retained as a compatibility/evidence hint and must not override project policy. If `release_finalization_policy` is omitted, or if required credential environment variables are missing, expect an explicit `missing_policy` or `missing_credentials` stop rather than a silent fallback.

When governor orchestration changes, also smoke-test the repeated-cycle shell with `agent-loop run-governor --epic-count 2` against a temp target repo. The shipped command composes repeated one-epic cycles and writes parent `governor.log`, `governor.raw.log`, and `events.jsonl` artifacts; child release evidence remains in the child run and is still inspectable. The remaining hardening work is in cleanup, richer state refresh, and broader repeated-epic consumption of the release-local stop policy, not in the loop scaffold itself.

Release-local convergence decisions should be inspectable in the same evidence trail. Required findings keep the release in the bounded repair loop until they are resolved, explicitly accepted with rationale, or stopped by retry budget or a hard gate. Duplicate, false-positive, and other soft findings should be accepted only with evidence-backed rationale and validators rerun, while scope-expansion and backlog-follow-up findings should be written up as proposals for the next planning cycle instead of being forced into the current release.

`auto_develop` self-development treats `src/agentic_devloop/release.py` overlap as a scheduling risk, not a configured unsafe overlap. Many orchestration epics must touch that file, so the expected guardrail is the overlap-risk report plus a typed release-scheduling decision, usually serialized execution, followed by verification and feature review. Keep generated artifacts, repo-state, contracts, objectives, lockfiles, migrations, and other configured exclusive paths as hard unsafe-overlap boundaries.

When `plan-release` or `run-objective` runs the execution-strategy seam, inspect the JSON output fields `execution_strategy_selection_path`, `supervisor_decision_path`, and `one_shot_execution_input_path` when present. The `one_shot_execution_input.json` artifact is only written for the `one_shot` action; it records the bounded objective scope, evidence requirements, stop conditions, and selector inputs that replace contract decomposition for that release. This is currently a planning artifact, not an executing worker path: `run-objective` can return `release: null` for a one-shot selection, and `run-backlog` intentionally defaults to executable contract decomposition until the one-shot runner is implemented. A `stop` strategy is recorded in `execution_strategy_selection.json`; typed supervisor decision persistence for blocked `stop` outcomes is still planned, so do not expect `supervisor_decision_path` for that case yet. Execution-strategy supervisor decisions store absolute evidence paths so state-review snapshots and selection artifacts can be reloaded without depending on the current working directory.

Feature review backend notes (implemented today):
- The reviewer executor uses Codex CLI (`executor.type: codex_cli`). `run-release` preflights the reviewer backend and blocks review immediately when `codex` is missing from `PATH` or the configured reviewer executor type is unsupported.
- When the reviewer backend is unsupported, missing, or returns invalid output, `feature_review.json` records an `escalate` decision with a critical finding that includes stable remediation hints (install/configure `codex`, verify `model_roles.reviewer`, or disable semantic review and use deterministic/human review).

Soft-gate exceptions are recorded in the evidence bundle or release root, not hidden in logs. Task-level findings live at `runs/<run-id>/<task-id>/evidence/soft_gate_decision.json`; release-level budget exceptions live at `runs/<run-id>/soft_gate_decisions.json`. Each record includes the finding identifier, severity, risk, recommended actions, evidence paths, decision, rationale, fallback plan, and `validators_rerun`. The rerun list is the reviewer/supervisor checklist for repeating or re-reading the relevant validators before the soft exception is treated as durable. Hard validation still comes first, and the reviewer loop remains bounded to one integrated feature branch per release. Typed supervisor decision records under `runs/<run-id>/**/supervisor_decisions/` are the implemented durable audit trail for the scheduler, repair, and soft-budget choices that support that loop, including `release_scheduling` decisions with `selected_action`, `fallback_plan`, `validators_to_rerun`, and strict staleness inputs. Loading them is strict rather than warning-only.

Verification-environment repair follows the same audit discipline. When a task fails because the shared verification runtime is stale or miswired, the release writes `verification_environment_repair_input.json` in the task bundle and a typed `environment_repair__<id>.json` decision under `runs/<run-id>/<task-id>/evidence/supervisor_decisions/`. The decision records `policy_basis`, `selected_policy_action`, `outcome`, `fallback_plan`, `source_evidence_paths`, `retry_budget_impact`, `validators_to_rerun`, and any refusal reason or capture commands needed to preserve the evidence trail. `apply_repair_and_retry` consumes one retry-budget slot and reruns the task's verification path; `capture_evidence_only` records the environment evidence without retrying; `stop` or `escalate` ends the loop with evidence. The rerun list must stay explicit and inspectable. This seam is only for verification-environment drift and bounded evidence capture, not dependency installation, network repair, credential repair, source-file mutation, or executor-liveness classification.

The implemented soft scope-risk policy uses the same audit trail for changed-file and diff-size overages. Those findings are supervisor-adjudicated only when the hard gates have already passed. `run-release` now emits a first-party typed `scope_risk_budget_policy` artifact in the same run when an overage is detected and no decision exists yet, then applies the scope-risk gate before finalization. The generated decision is intentionally non-accepting and keeps the release blocked until an explicit accepted decision is present. The hard-stop list remains non-negotiable: forbidden paths, generated artifacts, lockfiles, migrations, configured exclusive paths, out-of-scope files, missing evidence, unsafe finalization, and failed verification.

Generated-contract admission repair uses the same evidence discipline. When planner output fails deterministic admission for repairable reasons, the normalized artifact should carry the raw validation errors, the chosen repair action, derived evidence paths, and the validators that must be rerun after repair. Runtime-command normalization is intentionally narrow: direct `.venv/bin/python ...` and allowlisted env-prefixed forms (for example `PYTHONPATH=... .venv/bin/python ...`) can be rewritten to the configured shared runtime, while shell-operator forms or unknown env prefixes are refused. The release may continue only after the repaired contract passes deterministic validation; if the candidate still violates hard gates, the stop reason and admission evidence remain authoritative. Changed-file and diff-size overages are handled the same way as other scope-risk findings: they are supervisor-adjudicated only after hard gates pass. Reviewer- and supervisor-output normalization stay documented as planned extensions until their code and tests ship.

Legacy supervisor decision artifacts that predate `validators_to_rerun` are only
partially compatible. Selected decision types load with the sentinel
`legacy_schema_v1_validators_unspecified` so humans and agents can inspect old
evidence, but the sentinel is not runnable and is filtered out by
`effective_validators_to_rerun()`. Applied `model_output_normalization` retry
records require explicit concrete validators; old artifacts without them must be
backfilled or regenerated before they can drive autonomous retry behavior.

Supervisor decision artifacts are persisted as deterministic JSON files under `runs/<run-id>/supervisor_decisions/` with filenames `<decision_type>__<decision_id>.json`. Loading these artifacts is strict: schema validation failures and missing referenced evidence paths are hard errors, not warning-only conditions. This typed record trail is implemented; the one-epic execution-strategy seam is shipped, the planner-admission-repair path uses the same strict evidence-path and validator-rerun discipline, and reviewer/supervisor normalization extensions remain planned until code and tests explicitly ship them. The broader N-epic governor that would consume these artifacts across repeated epics remains planned.

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
