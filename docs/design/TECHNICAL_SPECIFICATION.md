# Technical Specification

## Tech Stack

Core runtime:

- Python 3.11+.
- YAML or TOML configuration files.
- JSON for machine-generated state and evidence metadata.
- Markdown for human-readable summaries and reviews.
- Git worktrees for isolated task execution.
- Subprocess-based execution for coding agents and deterministic tools.

Initial agent backend:

- Codex CLI as bounded executor.

Potential future backends:

- Aider.
- Claude Code.
- OpenHands.
- Other CLI coding agents.

## Storage

v1 uses filesystem state only. Do not add a database until run indexing and search become painful.

State includes:

- Run directories.
- Task contracts.
- Evidence bundles.
- Logs.
- Model call metadata.
- Generated summaries.
- Git branch and worktree metadata.

SQLite is acceptable later if filesystem search becomes a bottleneck.

## Primary Data Models

### ProjectConfig

Defines how the orchestrator interacts with a target repository.

```yaml
project_id: rust_rockfall
repo_path: /path/to/rust_rockfall
default_base_branch: main
worktree_root: /path/to/worktrees/rust_rockfall

executor:
  type: codex_cli
  model: gpt-5.3-codex
  max_walltime_minutes: 25
  fallback_models:
    - gpt-5.4-mini

model_catalog:
  strategic_planner:
    model: gpt-5.5
    capabilities: [strategic_planning, hard_debugging, semantic_review]
    budget_class: XL
    availability: supported
  runtime_supervisor:
    model: gpt-5.2
    capabilities: [runtime_supervision, review, long_running_stability]
    budget_class: L
    availability: unknown
  coding_worker:
    model: gpt-5.3-codex
    capabilities: [implementation, refactoring, tests]
    budget_class: M
    availability: unknown
  micro_repair:
    model: gpt-5.3-codex-spark
    capabilities: [conflict_repair, lint_repair, tiny_patches]
    budget_class: XS
    availability: unknown
  cheap_router:
    model: gpt-5.4-mini
    capabilities: [routing, summarization, fallback_worker]
    budget_class: S
    availability: supported

model_roles:
  worker:
    type: codex_cli
    model: gpt-5.3-codex
    max_walltime_minutes: 25
    fallback_models:
      - gpt-5.4-mini
  repair:
    type: codex_cli
    model: gpt-5.3-codex-spark
    max_walltime_minutes: 10
    fallback_models:
      - gpt-5.4-mini
  router:
    type: codex_cli
    model: gpt-5.4-mini
    max_walltime_minutes: 10
  reviewer:
    type: codex_cli
    model: gpt-5.2
    max_walltime_minutes: 20
    fallback_models:
      - gpt-5.5
  planner:
    type: codex_cli
    model: gpt-5.5
    max_walltime_minutes: 20

model_routing:
  default_role: worker
  task_type_roles:
    documentation: router
    scientific_validation: reviewer
    release_preparation: reviewer
  budget_class_roles:
    L: reviewer
    XL: planner
  escalation_role: reviewer

verification_profiles:
  default:
    commands:
      - cargo fmt --check
      - cargo clippy --all-targets --all-features -- -D warnings
      - cargo test --all-targets --all-features

budget:
  max_executor_attempts_per_task: 2
  max_strong_model_calls_per_release: 10
  max_changed_files_per_task: 8
  max_diff_lines_per_task: 600
  max_context_chars_per_task: 30000

repo_state_path: repo_state/rust_rockfall
```

`model_catalog` is advisory policy, not an execution backend. It lets `doctor` report whether configured roles point at supported, unsupported, or unproven models. `model_roles` remains the execution source of truth. If a primary model is unavailable, bounded executor attempts can fall back through `fallback_models`; conflict repair uses the `repair` role when configured.

### ReleaseObjective

A release-sized goal that must be decomposed before execution.

```yaml
release_id: v0.8.0
title: Major feature release
objective: >
  Implement a release-sized feature increment for rust_rockfall.
non_goals:
  - Do not rewrite core architecture without explicit approval.
  - Do not weaken validation gates.
acceptance_criteria:
  - All default verification checks pass.
  - Scientific assumptions are documented.
  - Evidence bundle exists for each accepted task.
  - Human approval before merge unless autonomous finalization is explicitly enabled.
```

### TaskContract

A bounded unit of execution.

```yaml
task_id: rr-0001
release_id: v0.8.0
title: Add regression test for selected validation gate report mismatch
task_type: scientific_validation
budget_class: M

objective: >
  Add one regression test covering the mismatch between selected gate evidence
  and generated report output.

allowed_files:
  - tests/**
  - scripts/validate_public_real_site_conditional_pilot_run.py

forbidden_changes:
  - Do not change validation schema.
  - Do not update benchmark fixtures.
  - Do not weaken existing assertions.

required_evidence:
  - git diff
  - test output
  - changed-files list
  - executor summary
  - verifier summary

verification:
  profile: scientific_validation

stop_conditions:
  - More than 8 files changed.
  - More than 600 diff lines.
  - Verification fails twice.
  - Agent proposes changing forbidden files.

scientific_assumptions:
  - No domain behavior changes are expected.
fixture_changes_allowed: false
tolerance_changes_allowed: false
benchmark_delta_required: false
```

### TaskRun

Machine state for one task execution.

```json
{
  "task_id": "rr-0001",
  "state": "VERIFYING",
  "worktree_path": "/tmp/agent-worktrees/rr-0001",
  "branch": "agent/v0.8.0/rr-0001",
  "executor_attempts": 1,
  "started_at": "2026-05-11T21:00:00+02:00",
  "updated_at": "2026-05-11T21:18:00+02:00",
  "changed_files": [],
  "diff_lines": 0,
  "verification_results": []
}
```

### EvidenceBundle

Immutable record of what happened.

```text
runs/
  2026-05-11_v0.8.0/
    rr-0001/
      contract.yaml
      run_state.json
      executor_prompt.md
      executor_stdout.log
      executor_stderr.log
      model_call_metadata.json
      executor_attempts.json
      failure_diagnosis.yaml
      git_diff.patch
      changed_files.txt
      verification.log
      scientific_review.yaml
      benchmark_delta.json
      remote_dispatch.yaml
      review.md
      decision.yaml
```

### ReviewDecision

```yaml
task_id: rr-0001
decision: accepted
reviewer: human | strong_model
rationale: >
  Diff is within contract. Verification passed. No forbidden files changed.
risks:
  - Regression test covers only one failure path.
follow_up_tasks:
  - Add negative fixture for malformed report evidence.
```

## CLI Interface

Current CLI shape:

```bash
agent-loop init --project rust_rockfall --repo ~/dev/rust_rockfall

agent-loop config \
  --project rust_rockfall

agent-loop config \
  --project rust_rockfall \
  --validate-repo

agent-loop run-task \
  --project rust_rockfall \
  --contract contracts/rr-0001.yaml

agent-loop run-task \
  --project auto_develop \
  --contract contracts/ad-0001.yaml \
  --push-on-accept

agent-loop run-release \
  --project auto_develop \
  --release sprint-0

agent-loop plan-release \
  --objective objectives/v0.8.0.yaml

agent-loop plan-release \
  --objective objectives/v0.8.0.yaml \
  --mode strong-model \
  --project auto_develop

agent-loop run-objective \
  --project auto_develop \
  --objective objectives/v0.8.0.yaml \
  --mode strong-model \
  --execute-planner

agent-loop status

agent-loop status --limit 5
```

Planned later commands:

- `agent-loop plan`
- `agent-loop run-next`
- `agent-loop verify`
- `agent-loop review`

### `run-task` Flow

`agent-loop run-task` currently performs the first end-to-end orchestration path:

1. Load and validate `ProjectConfig`.
2. Load and validate `TaskContract`.
3. Create a Git worktree and task branch.
4. Write `executor_prompt.md` from the task contract.
5. Run the configured executor.
6. Run task verification commands.
7. Collect evidence into `runs/<run-id>/<task-id>/evidence/`.
8. Run deterministic review.
9. Persist `decision.yaml` and `review.md`.
10. Optionally commit, merge, and push accepted changes when completion flags are set.

By default, the command does not merge changes or create a pull request. Autonomous completion is opt-in:

- `--commit-on-accept`: commit accepted task changes in the task worktree.
- `--merge-on-accept`: commit accepted task changes and merge the task branch into the base branch.
- `--push-on-accept`: commit, merge, and push the base branch to `origin`.

Finalization conflicts are captured in evidence. Contract-contained rebase conflicts get one bounded autonomous repair attempt; unresolved conflicts escalate.

### `run-release` Flow

`agent-loop run-release` is the first release-level orchestration path:

1. Load and validate `ProjectConfig`.
2. Fail fast unless the configured project `worktree_root` is empty.
3. Resolve an ordered contract queue from explicit `--contract` arguments or `repo_state/<project>/release_plan.yaml`.
4. Fail fast if any task branch for the selected release queue already exists.
5. Create or reuse the orchestrator-owned integration branch, defaulting to `feature/<release>`, from the configured base branch.
6. Classify `allowed_files` overlap.
7. Build an execution DAG from explicit `depends_on` edges and inferred overlap dependencies.
8. In sequential mode, run the queue in order; in parallel mode, submit currently-ready tasks, monitor completions, and dynamically submit newly unblocked tasks.
9. Base task branches on the integration branch and merge accepted task branches back into that integration branch when task finalization is requested.
10. Stop after the first non-accepted task unless `--continue-on-failure` is set.
11. Optionally finalize the accepted release with `--release-finalize merge-main`, `push-feature`, or `push-main`.
12. Mirror filtered progress to `runs/<release-run-id>/release.log` and full raw agent streams to `release.raw.log`.
13. Remove task worktrees and merged task branches unless `--debug-keep-artifacts` is set; preserve accepted unfinalized worktrees, unmerged accepted branches, and failed-finalization branches.
14. Persist `runs/<release-run-id>/release_summary.json` and `release_review.md`.

### Release Cleanup Command

`agent-loop cleanup --project <project> --release <release>` is a dry-run command that reports matching stale release artifacts:

- directories below the configured `worktree_root` whose names contain the release identifier;
- local task branches matching `agent/<release>/*`;
- optionally the `feature/<release>` integration branch when `--include-integration-branch` is passed.

`--force` removes the matching worktree directories and deletes the matching branches. The command refuses to remove paths outside the configured worktree root and refuses to delete the currently checked-out branch. The integration branch is preserved unless explicitly requested.

This command executes already-defined contracts. Objective-level planning and execution are composed by `run-objective`.

### `plan-release` Flow

`agent-loop plan-release` produces a validated contract plan:

1. Load and validate a `ReleaseObjective`.
2. Inspect existing contracts for the release.
3. Write `runs/<plan-id>/contract_plan.json`.
4. If no contracts exist, propose a planning-only release-preparation draft.
5. If contracts exist, emit acceptance-criteria coverage review entries.
6. In `--mode strong-model`, reserve a strong-model budget ledger entry and write `planner_prompt.md`.
7. With `--execute-planner`, run the configured planner backend, persist planner stdout/stderr/metadata paths, parse structured JSON output, and validate generated contracts.

Generated contracts must match the release ID, require diff evidence, include a scope or verification stop condition, and must not request whole-repo file scope. When project config is available, generated contracts must reference existing verification profiles, keep profile/task-type choices consistent, and must not exceed `budget.max_changed_files_per_task` allowed-file entries.

### `run-objective` Flow

`agent-loop run-objective` composes planning and release execution:

1. Run `plan-release` behavior for the supplied objective.
2. Write validated generated contracts to the configured contracts directory.
3. Pass those exact written contract paths to `run-release`.
4. Return both the planning artifact path and release summary.

### Generated Contract Review

The planner backend expects humans to review the generated planning artifacts before any new task contracts are admitted into a release queue:

1. Check `contract_plan.json` against the release objective and existing contract set.
2. If strong-model planning was requested, inspect `planner_prompt.md` for the draft inputs and release scope.
3. Confirm that any proposed follow-up contracts remain bounded by `allowed_files`, `forbidden_changes`, and the release budget.
4. Only then write or accept explicit task contracts and pass them to `run-release`.

### Finalization Locking

Accepted-task finalization:

1. Acquires `.git/agent-main.lock`.
2. Commits task worktree changes if needed.
3. Switches the base repository to the configured base branch.
4. Rebases the task worktree onto `origin/<base>` when available, otherwise local `<base>`.
5. Merges the task branch into base.
6. Pushes base when requested.

Contract-contained rebase conflicts:

1. Write `conflict_repair_prompt.md`.
2. Run one bounded repair worker attempt against conflicted files only.
3. Rerun verification.
4. Continue the rebase.
5. Retry finalization once.

Unresolved rebase conflicts and merge conflicts are persisted through `conflict_repair.yaml`, `finalization.yaml`, and an escalated decision.

`agent-loop status` reads existing evidence bundles and prints recent run summaries with run ID, task ID, decision, and bundle path.

### Failure-Diagnosis Flow

When `run-task` or release execution exhausts bounded executor attempts or hits a verification failure, the orchestrator records a failure diagnosis alongside the rest of the task evidence.

The diagnosis request includes:

- contract metadata;
- executor result and recorded attempts;
- verification results;
- changed files from the worktree;
- executor and verification log excerpts.

The default backend is deterministic. It classifies the failure from the recorded evidence and writes `failure_diagnosis.yaml` with the diagnosis category, confidence, evidence excerpts, recommendation, and retry or escalation guidance. The same backend seam can be replaced later with a model-backed reviewer, but the evidence shape should remain the same.

Before retrying or escalating a failed task, inspect:

- `failure_diagnosis.yaml`;
- `executor_attempts.json`;
- `verification.log`;
- `executor_stdout.log`;
- `executor_stderr.log`;
- `changed_files.txt`;
- `git_diff.patch`.

Use `guidance.retryable` and `guidance.escalate` as the primary control points for deciding whether to rerun the task, narrow scope, or hand the failure to stronger review.

### Domain Validation Evidence

For benchmark and domain-validation tasks, deterministic review records:

- Fixture-like file changes.
- Tolerance-like diff lines.
- Benchmark-like file changes.
- Policy violations caused by missing explicit permissions.

`remote_dispatch.yaml` records declared remote dispatch requirements when the target repository's own documentation or task contract requires remote execution. v1 records the request but does not execute remote jobs.

## Project Adapter Interface

```python
class ProjectAdapter:
    def load_config(self) -> ProjectConfig:
        ...

    def create_worktree(self, task: TaskContract) -> WorktreeRef:
        ...

    def build_executor_prompt(self, task: TaskContract, context: ContextBundle) -> str:
        ...

    def run_verification(self, task: TaskContract, worktree: WorktreeRef) -> VerificationResult:
        ...

    def collect_evidence(self, task: TaskContract, worktree: WorktreeRef) -> EvidenceBundle:
        ...
```

## Executor Interface

```python
class Executor:
    def run(self, prompt: str, worktree_path: Path, timeout: int) -> ExecutorResult:
        ...
```

Executor results must include:

- Standard output.
- Standard error.
- Exit code.
- Wall-clock duration.
- Model or backend identity when available.
- Generated summary when available.

## Reviewer Interface

```python
class Reviewer:
    def review(self, task: TaskContract, evidence: EvidenceBundle) -> ReviewDecision:
        ...
```

Review may be deterministic, model-based, human, strong-model-assisted, or hybrid. v1 should start with deterministic pre-review and human final review.

## Security and Auth

### State Handling

All orchestrator state is local filesystem state. No secrets may be written to evidence bundles. Evidence bundles may contain source code diffs and logs and must be treated as repository-sensitive.

### Identity

For v1, identity is local-user identity. No multi-user auth layer is required. Git author identity comes from local Git configuration.

### Data Protection

Required controls:

- Redact environment variables from logs.
- Avoid dumping the full shell environment.
- Avoid storing API keys in config.
- Use provider CLI authentication mechanisms instead of embedding credentials.
- Mark evidence bundles as non-public by default.

### Agent Permissions

Execution agents run in isolated Git worktrees.

Commands should run with:

- Explicit working directory.
- Explicit timeout.
- Bounded environment.
- No destructive filesystem permissions outside the worktree.

v1 subprocess isolation is not a sandbox. Later phases may add containers, restricted shells, allowlisted commands, ephemeral users, or filesystem sandboxing.

## Infrastructure

### Deployment Targets

v1 target:

- Local Mac with Apple Silicon as primary orchestrator host.

### Worktree Strategy

Each task gets:

- A dedicated Git branch.
- A dedicated Git worktree.
- A bounded contract.
- Isolated logs and evidence.

Worktree naming:

```text
agent-worktrees/
  rust_rockfall/
    rr-0001/
    rr-0002/
```

Branch naming:

```text
agent/<release-id>/<task-id>
```

## CI/CD Requirements

v1 should not depend on CI for core verification. Local deterministic verification must run before PR creation. CI is a second-level verification gate.

Required checks for Rust projects:

```bash
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test --all-targets --all-features
```

Optional project-specific checks:

- Benchmark scripts.
- Validation scripts.
- Documentation generation.
- Schema checks.
- Fixture consistency checks.

## Known Unknowns and Technical Risks

### Existing Orchestrator Reuse

There may be existing projects that already manage worktrees and coding-agent sessions.

Mitigation: design v1 as a policy wrapper that can later delegate worktree or session management to existing tools.

### Cost Accounting

Provider CLIs may not expose precise token usage.

Mitigation: track approximate call count, wall time, model identity, prompt size, and output size.

### Prompt Compression

Aggressive compression may remove domain-specific validation details.

Mitigation: never compress equations, validation rules, numerical tolerances, or benchmark definitions without review.

### Domain Review

Strong models can still approve invalid domain changes.

Mitigation: require deterministic evidence, explicit assumptions, and human approval for risky changes.
