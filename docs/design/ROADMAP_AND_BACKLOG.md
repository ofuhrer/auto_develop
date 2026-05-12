# Roadmap and Backlog

## Development Strategy

Build the smallest useful autonomous loop first:

1. Define strict task contracts.
2. Execute one task in one worktree.
3. Run deterministic verification.
4. Collect immutable evidence.
5. Produce a deterministic accept, reject, or escalate decision.

Defer databases, web UI, distributed execution, open-model serving, and implicit automatic merging until the local loop is reliable.

## Phase 1: MVP Bounded Autonomous Execution Loop

Goal:

Build a local external CLI orchestrator that can execute one bounded task at a time against a Git repository using worktrees, Codex CLI, deterministic verification, and evidence bundles.

Scope:

- External generic tool.
- Local Mac control plane.
- One target project config.
- One executor backend.
- Filesystem state.
- No database.
- No web UI.
- No full Balfrin integration.
- Autonomous merging only when explicitly requested after deterministic acceptance.

Required capabilities:

1. Load project config.
2. Load release objective.
3. Generate or ingest task contracts.
4. Create Git worktree and branch for a task.
5. Build executor prompt from stable template plus task contract.
6. Run Codex CLI with timeout.
7. Run deterministic verification commands.
8. Collect evidence bundle.
9. Produce accept, reject, or escalate recommendation.
10. Require human approval before merge unless autonomous finalization was explicitly requested.

Success criterion:

The system can complete a small synthetic release objective:

```text
Prepare v0.7.1: improve one validation report, add one regression test, update one doc page, no scientific behavior changes.
```

## Phase 2: Cost and Context Management

Goal:

Reduce cost and improve reliability by adding explicit context slicing, budget enforcement, and prompt reuse.

Scope:

- Stable prompt prefix.
- Externalized repo memory.
- Budget ledger.
- Run summaries.
- Task-local context retrieval.
- Model-routing policy.

Required capabilities:

1. Maintain repo state files:
   - Architecture summary.
   - Active constraints.
   - Known failures.
   - Benchmark status.
   - Release plan.
2. Inject only relevant context into executor prompts.
3. Track approximate prompt and output size.
4. Enforce maximum strong-model calls per release.
5. Enforce maximum executor attempts per task.
6. Detect repeated failure modes.
7. Escalate failed tasks to strong-model review instead of looping blindly.

Success criterion:

The system can execute multiple related tasks with lower repeated context and without manually copying long prompt histories.

Current implementation status:

- Repo-state files are supported through `repo_state_path`.
- `auto_develop` has canonical state files under `repo_state/auto_develop/`.
- Executor prompts inject selected external context from repo-state files.
- Context size is bounded by `budget.max_context_chars_per_task`.
- Evidence bundles persist `model_call_metadata.json` with prompt/output character counts.
- `agent-loop status` reads existing evidence bundles and reports recent run summaries.
- `agent-loop run-release` executes an ordered set of existing contracts from explicit `--contract` arguments or `repo_state/<project>/release_plan.yaml`.
- Release queues classify allowed-file overlap; minor overlap is sequential-only, broad overlap blocks parallel mode, and exact same concrete-file overlap is rejected.
- Project configs support `model_roles` and `model_routing` for cheap-worker and stronger-model task execution routing.
- Executor roles support `fallback_models`; attempts are bounded by `budget.max_executor_attempts_per_task`.
- Executor failure evidence includes `executor_attempts.json` and deterministic `failure_diagnosis.yaml`.
- `agent-loop plan-release` writes deterministic contract planning scaffolds from release objectives and can reserve strong-model planning budget while writing a planner prompt.
- Accepted-task finalization uses a local merge lock and rebases the worktree onto latest base before merging.
- Contract-contained rebase conflicts get one bounded autonomous repair attempt before escalation.
- Strong-model call accounting and model-based repeated-failure diagnosis are not yet implemented.

## Phase 3: Scientific Verification and Distributed Execution

Goal:

Extend the orchestrator for scientific software workflows involving benchmarks, experimental validation, heavy simulations, and optional Balfrin dispatch.

Scope:

- Validation profiles.
- Benchmark evidence.
- Scientific review checklists.
- Balfrin dispatch.
- Optional open/local model roles.
- Optional PR automation.

Required capabilities:

1. Define verification profiles by task type:
   - Code-only.
   - Documentation.
   - Benchmark.
   - Scientific validation.
   - Release preparation.
2. Require scientific assumptions in task contracts.
3. Detect fixture or tolerance changes.
4. Record benchmark deltas.
5. Dispatch heavy jobs to Balfrin when explicitly requested.
6. Collect remote logs and artifacts.
7. Support optional local or open models for safe low-risk tasks:
   - Log summarization.
   - Compiler-error explanation.
   - Code-location triage.
   - Low-risk documentation drafts.

Success criterion:

The system can support a release-sized objective such as `v0.7.0` to `v0.8.0` by decomposing it into bounded tasks and maintaining auditable evidence for each accepted change.

Current implementation status:

- Task contracts support task types: code-only, documentation, benchmark, scientific validation, and release preparation.
- Verification may reference a named project verification profile instead of repeating commands in each contract.
- Scientific validation and benchmark tasks require explicit scientific assumptions.
- Deterministic review detects unapproved fixture-like and tolerance-like changes.
- Evidence bundles persist `scientific_review.yaml`.
- Benchmark tasks or benchmark-like file changes persist `benchmark_delta.json`.
- Remote dispatch requests persist `remote_dispatch.yaml` with `declared_not_executed` status.
- Balfrin execution, remote artifact collection, local/open runtime adapters, strong-model planning, and PR automation are not yet implemented.

## Critical Path

### 1. Task Contract Schema

Everything depends on a precise contract. Without it, executor prompts become vague and agent drift returns.

Minimum fields:

- Task ID.
- Objective.
- Non-goals.
- Allowed files.
- Forbidden changes.
- Verification commands.
- Budget limits.
- Stop conditions.
- Required evidence.

### 2. Worktree Manager

Isolated worktrees are required before autonomous execution. Without worktrees, failed attempts pollute the main repository and make rollback expensive.

### 3. Evidence Bundle Format

Evidence collection must be implemented before review automation. Without evidence bundles, the system relies on agent summaries, which are not trustworthy.

### 4. Deterministic Verification Runner

Verification must be independent of the coding agent. The executor must not be allowed to redefine success.

### 5. State Machine

Task state transitions must be explicit. Without a state machine, the system will become an ad hoc script that recreates the original manual workflow.

### 6. Budget Governor

Cost control must be built into the first version. Retrofitting cost control later will be difficult because prompts, retries, and review calls will already be unconstrained.

## Edge Cases and Gotchas

### Agent Converts Scientific Task into Test-Passing Task

Risk:

The agent weakens scientific meaning while making tests pass.

Mitigation:

- Forbid validation weakening.
- Require scientific assumptions.
- Require benchmark evidence.
- Review fixture changes carefully.

### Agent Changes Too Many Files

Risk:

Large diffs are hard to review and increase hidden coupling.

Mitigation:

- Enforce maximum changed files.
- Enforce maximum diff lines.
- Stop on scope expansion.

### Agent Edits Fixtures or Tolerances

Risk:

The agent hides numerical problems by changing expected outputs.

Mitigation:

- Forbid fixture changes by default.
- Require explicit contract permission for tolerance changes.
- Record benchmark deltas.

### Agent Summaries Are Inaccurate

Risk:

The reviewer sees a plausible but false explanation.

Mitigation:

- Review diff, logs, and changed-files list.
- Treat summaries as auxiliary evidence only.

### Repeated Failure Loops Burn Tokens

Risk:

The executor keeps trying small variations after failure.

Mitigation:

- Limit executor attempts.
- Classify failures.
- Escalate to strong model or human after bounded retries.

### Strong Model Rate Limits

Risk:

The orchestration loop stalls if every task requires frontier reasoning.

Mitigation:

- Use strong models only at control points.
- Keep task execution cheap.
- Batch planning or review where possible.
- Cache stable context.

### Local or Open Models Add Operational Complexity

Risk:

Serving open models on Balfrin or Mac introduces latency, reproducibility, preemption, and quality variance.

Mitigation:

- Exclude open models from Phase 1.
- Later restrict them to low-risk summarization and triage tasks.

### Balfrin Dispatch Adds Complexity

Risk:

Remote execution introduces queueing, preemption, environment drift, and artifact collection issues.

Mitigation:

- Do not make Balfrin part of the initial control plane.
- Treat it as an optional execution target.
- Require explicit task contract fields for remote jobs.

### Existing Orchestrator Projects May Partially Overlap

Risk:

Building all worktree or session machinery from scratch may duplicate existing tools.

Mitigation:

- Keep v1 modular.
- Make executor and session runner replaceable.
- Implement project-specific policy separately from generic agent-running.

### Context Compression Removes Important Details

Risk:

Compression can destroy scientific assumptions or numerical constraints.

Mitigation:

- Compress logs and history, not equations or validation criteria.
- Keep canonical constraints in structured files.
- Inject exact validation rules uncompressed.

## Sprint 0

Sprint objective:

Create the smallest useful external orchestrator skeleton that can run one bounded Codex task in a Git worktree and collect evidence.

Current implementation status:

- Tasks 0.1 through 0.8 have an executable foundation.
- `agent-loop run-task` wires config loading, contract loading, worktree creation, prompt generation, executor invocation, verification, evidence collection, and deterministic review.
- `decision.yaml` and `review.md` are persisted inside the evidence bundle.
- Task 0.9 still requires a real `rust_rockfall` path and an actual low-risk Codex run.
- Task 0.10 should be written only after Task 0.9 produces real run evidence.

### Task 0.1: Create Repository Skeleton

Create external repo structure:

```text
agentic-devloop/
  pyproject.toml
  README.md
  src/agentic_devloop/
  configs/
  objectives/
  contracts/
  runs/
  tests/
```

Acceptance:

- Package installs locally.
- CLI entrypoint exists.
- Tests run.

### Task 0.2: Define Schemas

Implement Pydantic or dataclass schemas for:

- ProjectConfig.
- ReleaseObjective.
- TaskContract.
- TaskRun.
- EvidenceBundle.
- ReviewDecision.

Acceptance:

- Sample YAML files validate.
- Invalid missing fields fail clearly.

Implemented:

- Pydantic schemas in `src/agentic_devloop/models.py`.
- Sample YAML in `configs/`, `objectives/`, and `contracts/`.

### Task 0.3: Implement Project Config Loader

Add config support for `rust_rockfall`.

Acceptance:

- CLI can load and print normalized project config.
- Repo path validation works.
- Missing repo fails safely.

Implemented:

- `agent-loop config --project rust_rockfall`
- `agent-loop config --project rust_rockfall --validate-repo`

### Task 0.4: Implement Worktree Manager

Functions:

- Create worktree.
- Check clean state.
- Create branch.
- Remove failed worktree safely.

Acceptance:

- Creates task-specific worktree.
- Does not modify main worktree.
- Refuses to proceed if base repo is dirty unless explicitly allowed.

### Task 0.5: Implement Executor Wrapper

Implement subprocess wrapper for Codex CLI.

Acceptance:

- Takes prompt file.
- Runs in worktree.
- Enforces timeout.
- Captures standard output and standard error.
- Writes logs to run directory.

### Task 0.6: Implement Verification Runner

Run configured shell commands.

Acceptance:

- Records command, exit code, standard output, standard error, and duration.
- Stops on failure unless configured otherwise.
- Writes verification log.

### Task 0.7: Implement Evidence Collector

Collect:

- Contract.
- Run state.
- Executor prompt.
- Executor logs.
- Git diff.
- Changed files.
- Verification logs.

Acceptance:

- Evidence bundle is complete after one run.
- Bundle is immutable unless task is rerun with a new attempt ID.

### Task 0.8: Implement Deterministic Review Gate

Initial review rules:

- Verification passed.
- Changed files are within allowed paths.
- Forbidden files are untouched.
- Diff lines are below limit.
- Changed file count is below limit.

Acceptance:

- Produces `decision.yaml`.
- Rejects forbidden file changes.
- Rejects failed verification.

Implemented:

- Deterministic review in `src/agentic_devloop/review.py`.
- Decision persistence in `src/agentic_devloop/evidence.py`.

### Integration: `run-task`

`agent-loop run-task` now connects Tasks 0.2 through 0.8 into one command.

Example:

```bash
agent-loop run-task \
  --project rust_rockfall \
  --contract contracts/rr-0001.yaml
```

Before using this against a real repository, update `configs/rust_rockfall.yaml` with a real `repo_path` and `worktree_root`.

### Task 0.9: Run Synthetic Task

Use a low-risk task against `rust_rockfall`.

Example:

```text
Add or improve one documentation-only check without scientific behavior changes.
```

Acceptance:

- Worktree created.
- Codex executed.
- Verification run.
- Evidence collected.
- Decision produced.
- No automatic merge unless accepted-task finalization was explicitly requested.

### Task 0.10: Review Sprint 0

Manually inspect:

- Prompt quality.
- Evidence completeness.
- Token and cost estimate.
- Failure modes.
- Whether executor stayed within scope.

Output:

```text
SPRINT_0_REVIEW.md
```

Required sections:

- What worked.
- What failed.
- Where agent drift appeared.
- Where cost was wasted.
- Required changes before Phase 1.
