# Architecture

## Core Objective

Build an autonomous-first external control layer around existing coding agents. The orchestrator owns policy, task boundaries, state transitions, budgets, verification, evidence, roadmap governance, and explicitly configured finalization. A high-level runtime supervisor owns diagnosis, bounded repair, and continuation for contract-contained subsystem failures. Coding agents own implementation inside narrow contracts. Deterministic tools own acceptance evidence. Humans define goals, credentials, and hard safety constraints; they should not be routine approval gates inside the development loop.

The target operating model is intentionally terse for the human:

1. Provide one or two onboarding prompts for a freshly cloned `auto_develop` repo and the target repository.
2. Configure the target repository goal, repository instructions, credentials, and hard policy boundaries.
3. Invoke a high-level development loop with the number of epics to implement.
4. Let the governor and runtime supervisor select epics, decompose work, run workers, repair subsystem failures, update state, and continue.
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

The default posture is autonomous-first and agentic-first. Once a repository goal and safety policy are configured, the governor should first perform a repository state-review pass: inspect docs, roadmap state, repo-state memory, recent run summaries, release artifacts, metrics, tuning reports, current branch state, relevant source layout, and open work. It should then select the next highest-reward epic, decompose it, run bounded workers, verify, run an independent feature-review agent over the integrated feature branch, dispatch repair agents for reviewer findings, update state, and continue until the requested epic count, budget, or explicit stopping criteria are reached. The runtime supervisor should observe release events and evidence, classify recoverable failures, apply bounded repair actions, and resume execution. The implemented control plane is narrower than that target: it now has a one-epic `GovernorLoop` service boundary, a typed `StateStore` seam, a `RepairPolicy` seam, planner-output normalization, governor-level logging for `run-backlog`, runtime-supervisor repair/resume seams for structured release failures, deterministic state-review snapshot capture (`state_review_snapshot.json`) with contract-plan linkage (`state_review_snapshot_path`), and a release-local feature-review/repair loop when `model_roles.reviewer` is configured. That review loop is the shipped semantic gate for one integrated feature branch; it does not imply persistent governor memory or multi-epic review orchestration. Required findings become bounded repair contracts, the reviewer re-check reruns verification, and finalization is blocked until required findings are resolved or explicitly accepted with rationale. Full pre-epic state-review decisioning, persistent governor memory, broader N-epic loop, and always-on state refresh are planned extensions on top of those seams.

Human stopping points are exceptions, not workflow milestones:

- Supplying or changing the repository goal.
- Supplying credentials, secrets, or external permissions.
- Resolving scope changes that exceed configured policy.
- Resolving domain or validation changes that the target repo explicitly marks as non-autonomous.
- Reviewing repeated failures only after bounded strong-model diagnosis and repair attempts are exhausted.
- Approving destructive or irreversible operations that were not explicitly delegated.

Agents must not stop for routine implementation choices, formatting fixes, local verification, log collection, evidence packaging, flaky local tests, missing path-context in a generated contract, or a failed worker attempt when the issue is contract-contained and repairable. Those are inputs to autonomous diagnosis, repair, retry, and state update.

The deterministic kernel must remain strict about invariants: no skipped verification, no silent policy expansion, no broad file-scope changes, no unapproved destructive operations, and no acceptance based only on worker claims. The runtime supervisor may repair contracts, environment setup, task splits, and retry plans, but repaired artifacts must pass deterministic admission and verification before execution continues.

Planner output repair is part of the autonomous path. If a planner emits a useful but admission-invalid contract package, the system should not stop immediately. The supervisor should receive the planner output, validation errors, objective, config, schema, and repository policy, then apply bounded normalization that does not change task meaning: add required evidence such as `git diff` and changed-files lists, normalize worktree-local `.venv` verification commands to the configured shared runtime, repair schema spelling/shape drift, and preserve objective, allowed scope, forbidden changes, and stop conditions. The repaired package must then pass deterministic contract validation before execution continues. If normalization would broaden scope or change intent, the supervisor must stop with evidence.

Autonomy should reduce code where the code is only approximating judgment. The
kernel should expose facts and enforce invariants; the runtime supervisor should
own reasoning-heavy choices such as whether planner output is repairable, how to
split an over-budget contract, whether a long-running worker is active or stuck,
which model to escalate to, and how to update roadmap memory after a failed run.
Avoid growing deterministic heuristic code for those choices when a bounded
agent action plus a hard validator can produce the same safety outcome with less
maintenance.

The target control boundary is typed supervisor decisions, not more procedural
branches in the release runner. The kernel should emit overlap reports, budget
signals, reviewer findings, liveness signals, and validation results. A
supervisor agent should then write auditable decisions such as
`scheduling_decision`, `review_finding_decision`, `repair_budget_decision`,
`contract_normalization_decision`, or `environment_repair_decision`. Each
decision must include evidence paths, rationale, selected action, fallback plan,
and validators to rerun. The kernel applies only actions that remain inside
policy and pass deterministic validation.

### Runtime Supervisor Behavior

The implemented runtime supervisor accepts structured release events, release summaries, evidence bundles, raw logs, budget ledgers, tuning reports, and backlog-state references. It classifies recoverable failures into verification environment drift, planner contract non-normalization, task scope overbroad, release resumable, long-running worker active, model capability mismatch, repo-state stale, missing credentials, contract boundary violation, unsafe policy expansion, and exhausted retry budget.

- Retryable classifications only produce a retry decision while retry budget remains; once the budget is exhausted, the supervisor returns a stop decision with `exhausted_retry_budget`.
- Stop decisions carry structured stop evidence with the action kind, stop kind, and reason so the release can record why repair stopped.
- Planner normalization accepts `ContractPlan` values or plain mappings, revalidates them, and stops with hard-gate evidence if the candidate plan or generated contracts fail validation.
- Release resume requires `action_id`, `retry_budget`, and `stop_reason_fallback`; `retry_budget` must be non-negative, and resume is blocked if the task has already written files outside its allowed scope.
- Long-running worker inspection records a summary and active flag when the worker appears to still be running; it does not silently widen scope or bypass the retry budget.
- Repo-state updates are emitted as proposals with a summary and proposed changes; the supervisor does not write repo-state files directly.

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

Current implementation supports configurable task execution roles through `model_roles` and `model_routing`. Worker roles may define `fallback_models`, and every executor attempt is recorded in evidence. Release queues classify overlapping allowed-file scopes before execution. The current implementation still uses conservative overlap gates in places, but the target architecture treats overlap as a risk signal for a governor-owned execution DAG, not as an unconditional rejection rule. Minor overlap can run in parallel, sequence, or stack depending on the governor's dependency analysis; exact overlap in normal source files should usually require an explicit agent rationale and merge-repair plan rather than an automatic stop. Deterministic code should only hard-block overlap for paths that are unsafe by policy, such as generated artifacts, lockfiles, migrations, configured exclusive paths, destructive scripts, or files outside contract scope. If several generated contracts touch a normal source file such as `release.py`, autonomous execution should not require a human to split the batch manually. The scheduler should emit an overlap-risk report and the supervisor should choose a DAG: serialize, stack branches, narrow a contract, ask the planner to re-slice, or stop because an exclusive path policy is truly violated. In parallel mode the orchestrator builds a DAG from explicit `depends_on` fields and inferred overlap dependencies, submits ready tasks concurrently, monitors completions, and schedules newly unblocked tasks as outcomes arrive. Release-level planning supports deterministic scaffolding, strong-model budget reservation, explicit planner backend execution with planner stdout/stderr/metadata evidence, and `run-objective` composition from objective to generated contracts to release execution. Generated-contract admission rejects hard safety violations such as unsafe release IDs, missing diff evidence, weak stop conditions, and whole-repo file scope. Budget and size pressure should be recorded as soft or hard findings depending on severity; a reviewer/supervisor agent should decide whether to accept, split, rerun, or escalate soft violations. Accepted soft exceptions are written into task or release soft-gate artifacts with finding severity, risk, evidence paths, recommended actions, decision, rationale, fallback plan, and validators to rerun, but they do not relax any hard validator. The `RepairPolicy` seam classifies failures for the current one-epic loop, and the runtime supervisor now turns those classifications into bounded repair proposals, resume intents, inspection summaries, escalation recommendations, and repo-state update proposals while still deferring hard invariant enforcement to deterministic validators.

Some deterministic subsystems should become thinner after the supervisor exists:
deterministic backlog scoring becomes fallback scaffolding, failure diagnosis
becomes evidence packaging plus typed categories, budget tuning becomes numeric
ledger generation, overlap analysis becomes a risk report rather than a full
recovery policy, and human log formatting becomes a projection of structured
events.

The boundary is:

- hard deterministic invariants: Git isolation; forbidden paths; generated artifacts; missing required evidence; destructive-operation policy; credential/network policy; unsafe finalization or final merge target protection; and unrepaired verification failures;
- soft agent-governed findings: modest budget overages, source-file overlap, task splitting, model escalation, retry versus abandon, environment repair, and whether a cohesive verified diff should be accepted despite a sizing warning.

A soft override must write evidence containing the finding, severity, risk, evidence paths, decision, rationale, fallback plan, and validators rerun. It must not bypass hard validators; it can only choose a repair path or accept a soft exception.

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

Release runs write a human-cockpit `release.log` for live monitoring and retain full raw agent streams in `release.raw.log`. The cockpit log is curated and styled for `tail -f`: it uses ANSI color plus emojis, reports task objectives, allowed scope, executor attempts and selected models, long-running worker heartbeats, verification, review decisions, finalization, intervention hints, and a final release summary. Arbitrary worker stderr, plugin warnings, code snippets, and test literals stay out of `release.log`; full worker stdout/stderr remains available in the raw log for audit and debugging. Long-running workers should be classified from multiple signals, not elapsed time alone: process liveness, stdout/stderr activity, file/diff activity, executor heartbeat age, wall-clock budget, and tool/model events when available. The runtime supervisor should distinguish active work, quiet-but-alive work, stalled work, hung processes, and environment-blocked execution before deciding to wait, inspect, restart, escalate, or stop. A release refuses to start when the configured project worktree root already contains worktrees or selected task branches already exist. Release cleanup removes task worktrees plus merged task branches by default. Accepted unfinalized worktrees, unmerged accepted branches, and failed-finalization branches are preserved so accepted work remains reachable. Debug mode can retain all artifacts when post-mortem inspection is needed. Each release also writes `release_review.md`, `release_metrics.json`, `release_budget.json`, and `release_tuning.md`.

The multi-epic governor needs a parent log above release logs:

```text
runs/<governor-run-id>/governor.log
runs/<governor-run-id>/governor.raw.log
runs/<governor-run-id>/events.jsonl
```

Operators should be able to `tail -f governor.log` while the system selects, plans, normalizes, executes, repairs, finalizes, records learnings, and moves to the next epic. Per-release logs remain child artifacts, but the governor log is the single human-facing cockpit for the whole N-epic run.

`release_metrics.json` is the cost-analysis artifact. It records per-task prompt characters, context characters, output characters, executor attempt count, model attempt totals, verification time, changed-file counts, and diff size. These metrics are deliberately provider-agnostic character-count proxies until model usage metadata is available from the executor backend. `release_budget.json` captures the budget ledger, including usage, task summaries, model attempts, task-size outliers, verification bottlenecks, and waste signals. `release_tuning.md` turns that ledger into next-run guidance for routing and task sizing.

## Context Controls

Project state must be externalized. Do not keep long-lived state primarily inside model conversation history.

Autonomous systems fail quickly when context grows without discipline. Keep memory layered:

- Immutable project memory: authoritative architecture, API contracts, domain rules, and operational constraints.
- Dynamic episodic memory: compressed summaries of recent runs, unresolved failures, branch state, and open TODOs.
- Retrieval memory: searchable prior commits, logs, failures, and discussions, retrieved only when relevant.
- Working memory: the current task contract plus the smallest necessary context slice.

The roadmap governor is responsible for keeping dynamic memory, roadmap, and backlog state current. Before selecting an epic it should refresh its state view from tracked repo-state memory plus live repository evidence: branch state, current source layout, recent run summaries, release review artifacts, release metrics, budget ledgers, tuning reports, validation outputs, and changed documentation. After each epic or failed attempt it should inspect evidence artifacts, release metrics, tuning reports, validation outputs, and changed documentation, then propose or apply bounded roadmap/backlog updates according to configured policy. For simulation software, new scientific or domain findings are not side notes; they are first-class inputs to the next planning cycle.

### Agentic Feature Review

When `model_roles.reviewer` is configured, the release path includes an independent reviewer agent after worker branches have been integrated into `feature/<release>` and before PR creation, merge-to-main, or policy-approved autonomous finalization. This reviewer is distinct from worker agents and receives the objective, contracts, changed diff (`main..feature/<release>` or base branch equivalent), release evidence, verification logs, soft-gate decisions, documentation changes, and architecture constraints. It emits structured findings with severity, affected files, evidence references, required or optional repair actions, and a final recommendation.

Reviewer findings then feed a repair loop. Repair agents work on bounded review-repair contracts against the feature branch, verification reruns, and the reviewer agent re-checks unresolved findings. The loop stops when no required findings remain unresolved, or when it reaches an explicit blocked state (retry budget exhausted, hard invariants fail, credentials/policy missing, or configured human escalation). The deterministic `release_review.md` remains an evidence summary; `feature_review.json` and `feature_review_recheck.json` are the semantic reviewer artifacts. This is a release-scoped review/repair boundary, not a persistent governor memory loop and not a substitute for the planned N-epic orchestration layer.

Review repair loops must have convergence semantics. Repeated
review-fix-review cycles are acceptable only while findings remain blocking,
in-scope, and materially tied to the release objective. After a bounded number
of cycles, the supervisor should classify each new finding as a hard blocker,
soft finding, duplicate, false positive, scope expansion, or backlog follow-up.
Blocking correctness and hard-invariant findings continue to repair or stop.
Verified false positives can be accepted with rationale. Non-blocking quality
ideas should be recorded in governor memory/backlog instead of keeping the
feature branch hostage. This prevents infinite reviewer churn while preserving
auditability.

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

## Artifact Ownership

`auto_develop` has two roles that must not be confused:

- controller implementation: the `auto_develop` source repository and installed CLI;
- target control plane: the target repository's durable development memory, objectives, contracts, and run artifacts.

For self-development, both roles point at the same Git repository, so storing `repo_state/auto_develop`, `objectives/`, and `contracts/` in this repository is correct. For any external target repository, target-specific artifacts should not be committed to the `auto_develop` source repository. They should live in the target repository or in a dedicated target-control repository.

Recommended external-target layout:

```text
target_repo/
  .auto_develop/
    repo_state/<project>/      # tracked durable memory
    objectives/                # tracked selected/reusable release objectives
    contracts/                 # tracked accepted task contracts
    runs/                      # ignored or externally archived raw evidence
```

Durable and tracked:

- project-specific `repo_state`;
- selected objectives that define intended work;
- accepted/generated contracts that define executed work;
- compact release and epic outcome summaries needed for future backlog planning after controller checkout deletion;
- roadmap/backlog updates and active constraints.

Generated and usually untracked:

- raw `runs/` evidence, worker logs, prompts, diffs, and scratch files;
- temporary worktrees;
- executor caches and virtual environments.

The current CLI supports this layout by passing explicit `--contracts-dir`, `--objectives-dir`, and `--runs-dir` paths and by configuring `repo_state_path` relative to the target repository when the controller does not have a matching path. The target architecture should make this less error-prone by adding project-configured artifact directories and by persisting compact release and epic outcome state into tracked `repo_state`, so deleting and recloning the controller checkout does not lose the target project's development memory or the ability to choose the next epic.

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
