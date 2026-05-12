# Architecture

## Core Objective

Build an external control layer around existing coding agents. The orchestrator owns policy, task boundaries, state transitions, budgets, verification, and explicitly requested finalization. Coding agents own implementation inside narrow contracts. Deterministic tools own acceptance evidence. Humans or strong review models own release decisions and any merge or push that was not explicitly delegated to the orchestrator.

## Target Use Case

Initial target repositories include scientific software projects such as `rust_rockfall`:

- Rust project using Cargo.
- GitHub repository: `github.com/ofuhrer/rust_rockfall`.
- Requires tests, validation scripts, benchmark awareness, and scientific review gates.

## Architecture Pattern

The v1 architecture is intentionally small:

- CLI-driven orchestration tool.
- Filesystem-backed run state.
- Git worktrees for isolated task execution.
- Subprocess-based invocation of coding agents and deterministic tools.
- Plugin-style project adapters.
- Deterministic verification pipeline.
- Optional integration with existing worktree or session managers.

Do not build an unconstrained autonomous agent loop for v1. Use a state machine for task execution and DAGs only for release planning or validation dependencies.

## Autonomy Policy

The system should be fully autonomous within a bounded task phase. Once a task contract is accepted, the orchestrator should proceed through worktree creation, execution, verification, evidence collection, deterministic review, and retry handling without asking for minor decisions.

Human stopping points should be rare and meaningful:

- Approving release objectives and task contracts.
- Resolving scope changes that exceed the contract.
- Resolving scientific or validation changes.
- Reviewing repeated failures after bounded retries.
- Approving release operations.
- Approving merge or push operations unless the run was started with explicit autonomous finalization flags.

Agents must not stop for routine implementation choices, formatting fixes, local verification, log collection, or evidence packaging.

## Execution State Machine

Each task progresses through explicit states:

```text
PLANNED
-> CONTRACT_WRITTEN
-> WORKTREE_CREATED
-> EXECUTING
-> VERIFYING
-> REVIEWING
-> ACCEPTED | NEEDS_REVISION | FAILED | ESCALATED
```

Rules:

- No task may skip verification.
- No task may self-expand scope.
- No task may merge or push itself unless the run explicitly enabled accepted-task finalization.
- Failed verification may trigger bounded retries.
- Repeated failure escalates to strong-model or human diagnosis.

## Agent Drift Controls

Agents must not receive broad release objectives directly.

Bad:

```text
Improve the validation system.
```

Acceptable:

```yaml
objective: Add one regression test for mismatch between selected gate evidence and report output.
allowed_files:
  - tests/test_public_real_site_conditional_pilot_run.py
  - scripts/validate_public_real_site_conditional_pilot_run.py
forbidden_changes:
  - validation schema changes
  - weakening existing assertions
verification:
  - cargo test
  - python scripts/validate_public_real_site_conditional_pilot_run.py --check
```

## Cost Controls

The orchestrator must track and enforce:

- Maximum executor attempts per task.
- Maximum wall-clock time per task.
- Maximum changed files per task.
- Maximum diff lines per task.
- Maximum context size per model call.
- Maximum strong-model calls per release.

Default routing policy:

```text
cheap deterministic checks first
-> cheap or local model if safe
-> mid-tier coding model for bounded implementation
-> frontier model only for planning, review, or failure diagnosis
```

Current implementation supports configurable task execution roles through `model_roles` and `model_routing`. Worker roles may define `fallback_models`, and every executor attempt is recorded in evidence. Release queues classify overlapping allowed-file scopes before execution: minor overlap becomes a sequencing dependency, broad overlap blocks parallel mode, and exact same concrete-file overlap is rejected. In parallel mode the orchestrator builds a DAG from explicit `depends_on` fields and inferred overlap dependencies, submits ready tasks concurrently, monitors completions, and schedules newly unblocked tasks as outcomes arrive. Release-level planning supports deterministic scaffolding, strong-model budget reservation, explicit planner backend execution with planner stdout/stderr/metadata evidence, and `run-objective` composition from objective to generated contracts to release execution. Generated-contract admission rejects unsafe release IDs, missing diff evidence, weak stop conditions, whole-repo file scope, unknown or inconsistent verification profiles, and allowed-file counts above project budget. Strong-model review and model-based failure diagnosis are still explicit future control points rather than active automated calls.

The `doctor` command is the preflight entry point for release governance. It checks repo cleanliness, dirty working trees, stale worktree-root entries, existing integration or task branches for a release, and model-routing warnings before a governed release starts.

Merge finalization uses a local lock, rebases the task worktree onto the orchestrator-owned integration branch, then merges the accepted task branch into that feature branch. The feature branch, not `main`, is the release integration unit. Release finalization can then merge the feature branch into `main`, push the feature branch for PR review, or merge and push `main`. Contract-contained rebase conflicts get one bounded autonomous repair attempt followed by verification and one finalization retry. Remaining conflicts are surfaced as evidence.

Release runs write a filtered multiplexed `release.log` for live monitoring and retain full raw agent streams in `release.raw.log`. The filtered log is activity-oriented: it reports task objectives, allowed scope, executor attempts and selected models, verification, review decisions, finalization, and a final release summary. Full worker stdout/stderr remains available in the raw log for audit and debugging. A release refuses to start when the configured project worktree root already contains worktrees or selected task branches already exist. Release cleanup removes task worktrees plus merged task branches by default. Accepted unfinalized worktrees, unmerged accepted branches, and failed-finalization branches are preserved so accepted work remains reachable. Debug mode can retain all artifacts when post-mortem inspection is needed. Each release also writes `release_review.md`, `release_metrics.json`, `release_budget.json`, and `release_tuning.md`.

`release_metrics.json` is the cost-analysis artifact. It records per-task prompt characters, context characters, output characters, executor attempt count, model attempt totals, verification time, changed-file counts, and diff size. These metrics are deliberately provider-agnostic character-count proxies until model usage metadata is available from the executor backend. `release_budget.json` captures the budget ledger, including usage, task summaries, model attempts, task-size outliers, verification bottlenecks, and waste signals. `release_tuning.md` turns that ledger into next-run guidance for routing and task sizing.

## Context Controls

Project state must be externalized. Do not keep long-lived state primarily inside model conversation history.

Required state files:

```text
repo_state/
  architecture_summary.md
  active_constraints.yaml
  benchmark_status.json
  known_failures.md
  release_plan.yaml
```

Only relevant slices may be injected into task prompts. Compression is allowed for logs and history, but not for equations, validation rules, numerical tolerances, benchmark definitions, or task acceptance criteria.

## Scientific Verification Constraints

For scientific software repositories:

- Tests passing is necessary but not sufficient.
- Documentation must reflect implementation, not justify it retrospectively.
- Validation fixtures must not be changed unless explicitly allowed.
- Numerical tolerances must be justified.
- Benchmark changes must be explained.
- Scientific assumptions must be recorded in the task contract.

## Known Weak Points

- The architecture still depends on agent honesty in summaries unless summaries are cross-checked against diffs and logs.
- The state-machine approach reduces drift but does not eliminate invalid scientific reasoning.
- Filesystem-based state is simple but may become hard to query after many runs.
- Cost reduction may conflict with scientific rigor if cheaper models are used for judgment tasks.
- Existing worktree orchestrators may reduce implementation effort but may not provide scientific verification semantics.
