# Architecture

## Core Objective

Build an autonomous-first external control layer around existing coding agents. The orchestrator owns policy, task boundaries, state transitions, budgets, verification, evidence, roadmap governance, and explicitly configured finalization. Coding agents own implementation inside narrow contracts. Deterministic tools own acceptance evidence. Humans define goals, credentials, and hard safety constraints; they should not be routine approval gates inside the development loop.

The target operating model is intentionally terse for the human:

1. Provide one or two onboarding prompts for a freshly cloned `auto_develop` repo and the target repository.
2. Configure the target repository goal, repository instructions, credentials, and hard policy boundaries.
3. Invoke a high-level development loop with the number of epics to implement.
4. Let the governor select epics, decompose work, run workers, repair subsystem failures, update state, and continue.
5. Stop only for major problems: exhausted autonomous repair, missing credentials, unsafe policy expansion, destructive operations not explicitly delegated, or no actionable work remaining.

## Target Use Case

Initial target repositories include validation-heavy software projects such as `rust_rockfall`:

- Rust project using Cargo.
- GitHub repository: `github.com/ofuhrer/rust_rockfall`.
- Requires tests, validation scripts, benchmark awareness, and domain-specific review gates defined by the target repo.

## Architecture Pattern

The v1 architecture is intentionally small:

- CLI-driven orchestration tool.
- Filesystem-backed run state.
- Git worktrees for isolated task execution.
- Subprocess-based invocation of coding agents and deterministic tools.
- Plugin-style project adapters.
- Deterministic verification pipeline.
- Optional integration with existing worktree or session managers.

Do not build an unconstrained autonomous agent loop. Use explicit state machines and DAGs so autonomy is inspectable, restartable, and bounded.

## Autonomy Policy

The default posture is autonomous-first and agentic-first. Once a repository goal and safety policy are configured, the governor should read docs and roadmap state, select the next highest-reward epic, decompose it, run bounded workers, verify, review, update state, and continue until the requested epic count, budget, or explicit stopping criteria are reached. The implemented control plane is narrower than that target: it now has a one-epic `GovernorLoop` service boundary, a typed `StateStore` seam, and a `RepairPolicy` seam for bounded failure decisions. The broader N-epic loop and always-on state refresh are planned extensions on top of those seams.

Human stopping points are exceptions, not workflow milestones:

- Supplying or changing the repository goal.
- Supplying credentials, secrets, or external permissions.
- Resolving scope changes that exceed configured policy.
- Resolving domain or validation changes that the target repo explicitly marks as non-autonomous.
- Reviewing repeated failures only after bounded strong-model diagnosis and repair attempts are exhausted.
- Approving destructive or irreversible operations that were not explicitly delegated.

Agents must not stop for routine implementation choices, formatting fixes, local verification, log collection, evidence packaging, flaky local tests, missing path-context in a generated contract, or a failed worker attempt when the issue is contract-contained and repairable. Those are inputs to autonomous diagnosis, repair, retry, and state update.

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
- Repeated failure escalates first to stronger-model diagnosis or repair; human escalation is the last resort.

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

Current implementation supports configurable task execution roles through `model_roles` and `model_routing`. Worker roles may define `fallback_models`, and every executor attempt is recorded in evidence. Release queues classify overlapping allowed-file scopes before execution: minor overlap becomes a sequencing dependency, broad overlap blocks parallel mode, and exact same concrete-file overlap is rejected. In parallel mode the orchestrator builds a DAG from explicit `depends_on` fields and inferred overlap dependencies, submits ready tasks concurrently, monitors completions, and schedules newly unblocked tasks as outcomes arrive. Release-level planning supports deterministic scaffolding, strong-model budget reservation, explicit planner backend execution with planner stdout/stderr/metadata evidence, and `run-objective` composition from objective to generated contracts to release execution. Generated-contract admission rejects unsafe release IDs, missing diff evidence, weak stop conditions, whole-repo file scope, unknown or inconsistent verification profiles, and allowed-file counts above project budget. The new `RepairPolicy` seam classifies failures for the current one-epic loop, but the full autonomous repair strategy across repeated epics is still a future control point.

## Model Policy

Model quality for autonomous development is operational, not just benchmark-based. A stronger model can still be the wrong default if it drifts during long loops, burns quota quickly, or retries noisy edits. The project config therefore separates model capability policy from routing mechanics:

- `model_catalog` records known models, intended capabilities, budget class, and availability.
- `model_roles` binds orchestration roles to concrete executor models and fallbacks.
- `model_routing` maps task types and budget classes to roles.

Recommended default hierarchy:

```text
strategic planner:      gpt-5.5
runtime/review control: gpt-5.2
coding worker:          gpt-5.3-codex
micro repair:           gpt-5.3-codex-spark
cheap routing/fallback: gpt-5.4-mini
```

The current runtime orchestrator remains deterministic Python. It owns state transitions, worktrees, DAG scheduling, finalization, evidence, and budget checks. Model-backed runtime supervision is a future extension point; it should consume the same bounded state and evidence rather than replacing deterministic governance.

The `doctor` command is the preflight entry point for release governance. It checks repo cleanliness, dirty working trees, stale worktree-root entries, existing integration or task branches for a release, and model-routing warnings before a governed release starts.

Merge finalization uses a local lock, rebases the task worktree onto the orchestrator-owned integration branch, then merges the accepted task branch into that feature branch. The feature branch, not `main`, is the release integration unit. Release finalization can then merge the feature branch into `main`, push the feature branch for PR review, or merge and push `main`. Contract-contained rebase conflicts get one bounded autonomous repair attempt followed by verification and one finalization retry. Remaining conflicts are surfaced as evidence.

Release runs write a human-cockpit `release.log` for live monitoring and retain full raw agent streams in `release.raw.log`. The cockpit log is curated and styled for `tail -f`: it uses ANSI color plus emojis, reports task objectives, allowed scope, executor attempts and selected models, long-running worker heartbeats, verification, review decisions, finalization, intervention hints, and a final release summary. Arbitrary worker stderr, plugin warnings, code snippets, and test literals stay out of `release.log`; full worker stdout/stderr remains available in the raw log for audit and debugging. A release refuses to start when the configured project worktree root already contains worktrees or selected task branches already exist. Release cleanup removes task worktrees plus merged task branches by default. Accepted unfinalized worktrees, unmerged accepted branches, and failed-finalization branches are preserved so accepted work remains reachable. Debug mode can retain all artifacts when post-mortem inspection is needed. Each release also writes `release_review.md`, `release_metrics.json`, `release_budget.json`, and `release_tuning.md`.

`release_metrics.json` is the cost-analysis artifact. It records per-task prompt characters, context characters, output characters, executor attempt count, model attempt totals, verification time, changed-file counts, and diff size. These metrics are deliberately provider-agnostic character-count proxies until model usage metadata is available from the executor backend. `release_budget.json` captures the budget ledger, including usage, task summaries, model attempts, task-size outliers, verification bottlenecks, and waste signals. `release_tuning.md` turns that ledger into next-run guidance for routing and task sizing.

## Context Controls

Project state must be externalized. Do not keep long-lived state primarily inside model conversation history.

Autonomous systems fail quickly when context grows without discipline. Keep memory layered:

- Immutable project memory: authoritative architecture, API contracts, domain rules, and operational constraints.
- Dynamic episodic memory: compressed summaries of recent runs, unresolved failures, branch state, and open TODOs.
- Retrieval memory: searchable prior commits, logs, failures, and discussions, retrieved only when relevant.
- Working memory: the current task contract plus the smallest necessary context slice.

The roadmap governor is responsible for keeping dynamic memory, roadmap, and backlog state current. After each epic or failed attempt it should inspect evidence artifacts, release metrics, tuning reports, validation outputs, and changed documentation, then propose or apply bounded roadmap/backlog updates according to configured policy. For simulation software, new scientific or domain findings are not side notes; they are first-class inputs to the next planning cycle.

Required state files:

```text
repo_state/
  architecture_summary.md
  active_constraints.yaml
  benchmark_status.json
  known_failures.md
  release_plan.yaml
  backlog_state.yaml
```

Only relevant slices may be injected into task prompts. Compression is allowed for logs and history, but not for equations, validation rules, numerical tolerances, benchmark definitions, or task acceptance criteria.

## Repository Validation Constraints

For validation-heavy repositories:

- Tests passing is necessary but not sufficient.
- Documentation must reflect implementation, not justify it retrospectively.
- Validation fixtures must not be changed unless explicitly allowed.
- Numerical tolerances must be justified.
- Benchmark changes must be explained when benchmarks are part of the target repo workflow.
- Domain assumptions must be recorded in the task contract when the target repo workflow requires them.

## Known Weak Points

- The architecture still depends on agent honesty in summaries unless summaries are cross-checked against diffs and logs.
- The state-machine approach reduces drift but does not eliminate invalid domain reasoning.
- Filesystem-based state is simple but may become hard to query after many runs.
- Cost reduction may conflict with domain rigor if cheaper models are used for judgment tasks.
- Existing worktree orchestrators may reduce implementation effort but may not provide the target repo's validation semantics.
