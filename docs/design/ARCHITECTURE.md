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

Current implementation supports configurable task execution roles through `model_roles` and `model_routing`. Worker roles may define `fallback_models`, and every executor attempt is recorded in evidence. Release queues are checked for overlapping allowed-file scopes before execution. Release-level planning exists as deterministic scaffolding with strong-model budget reservation and prompt artifacts; strong-model review and model-based failure diagnosis are still explicit future control points rather than active automated calls.

Merge finalization uses a local lock, rebases the task worktree onto the latest available base branch, then merges into `main`. Rebase or merge conflicts are surfaced as finalization evidence rather than repaired automatically.

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
