# Roadmap and Backlog

## Development Strategy

Build the smallest useful autonomous loop first:

1. Define strict task contracts.
2. Execute one task in one worktree.
3. Run deterministic verification.
4. Collect immutable evidence.
5. Produce a deterministic accept, reject, or escalate decision.

Defer databases, web UI, distributed execution, and open-model serving until the local loop is reliable. Avoid human-in-the-loop gates unless they are explicit repository policy or safety boundaries.

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
- No project-specific remote execution integration.
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
10. Support autonomous finalization when explicitly configured by repository policy.

Success criterion:

The system can complete a small synthetic release objective:

```text
Prepare v0.7.1: improve one validation report, add one regression test, update one doc page, no domain behavior changes.
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

- Repo-state files are supported through `repo_state_path`, with controller-repo resolution for external target repositories.
- `auto_develop` has canonical state files under `repo_state/auto_develop/`.
- Executor prompts inject selected external context from repo-state files.
- Context size is bounded by `budget.max_context_chars_per_task`.
- Evidence bundles persist `model_call_metadata.json` with prompt/output character counts.
- `agent-loop doctor` reports repo cleanliness, stale worktrees, release-branch collisions, and routing warnings before a governed release starts.
- `agent-loop status` reads existing evidence bundles and reports recent run summaries.
- `agent-loop run-release` executes existing contracts from explicit `--contract` arguments or `repo_state/<project>/release_plan.yaml`.
- Parallel release mode builds a dynamic execution DAG from explicit dependencies and file-overlap dependencies, submits ready tasks concurrently, and schedules newly unblocked tasks as results arrive.
- Release runs use an orchestrator-owned integration branch by default (`feature/<release>`); accepted task branches merge into that feature branch before optional final merge or push.
- `agent-loop run-release` writes an activity-oriented multiplexed `release.log` for monitoring with `tail -f`, appends a final release summary, and preserves full raw agent streams in `release.raw.log`.
- Release runs write `release_metrics.json`, `release_budget.json`, and `release_tuning.md`; the budget ledger records model-attempt, context, prompt, output, verification, diff, and changed-file metrics, while the tuning report turns those signals into next-run routing and task-sizing guidance.
- Release runs write a `release_review.md` artifact.
- Release runs fail fast when the configured project worktree root contains stale worktrees or selected task branches already exist.
- Release task worktrees and merged branches are cleaned up by default unless debug artifact retention is requested; accepted unfinalized worktrees, unmerged accepted branches, and failed-finalization branches are preserved.
- Manual recovery is supported by `agent-loop cleanup`, which dry-runs by default and can remove stale release worktrees plus `agent/<release>/*` branches with `--force`.
- Release queues classify allowed-file overlap; minor overlap becomes a dependency, broad overlap blocks parallel mode, and exact same concrete-file overlap is rejected.
- Project configs support `model_roles`, `model_routing`, and `model_catalog` for capability-aware routing. Default configs express the recommended hierarchy of `gpt-5.5` strategic planning, `gpt-5.2` runtime/review supervision, `gpt-5.3-codex` coding work, `gpt-5.3-codex-spark` micro repair, and `gpt-5.4-mini` cheap routing/fallback. `doctor` reports unsupported, unknown, and uncataloged role models so routing can degrade safely.
- Executor roles support `fallback_models`; attempts are bounded by `budget.max_executor_attempts_per_task`.
- Repeated-failure diagnosis writes `executor_attempts.json` and `failure_diagnosis.yaml` after bounded executor or verification failures; the default backend is deterministic and the diagnosis step is exposed through a replaceable seam for stronger review.
- `agent-loop plan-release` writes deterministic contract planning scaffolds from release objectives and can execute a configured planner backend with `--execute-planner`, preserving planner stdout/stderr/metadata paths in the contract plan.
- `agent-loop run-objective` plans from an objective, writes validated generated contracts, and runs those contracts as a release queue.
- `agent-loop plan-backlog` analyzes the roadmap against a repository goal, emits prioritized epics, and can write the highest-priority epic as a release objective for `run-objective`.
- Generated-contract admission rejects release mismatch, missing diff evidence, weak stop conditions, whole-repo scope, unknown verification profiles, inconsistent verification profiles, and allowed-file counts above project budget.
- Accepted-task finalization uses a local merge lock and rebases the worktree onto latest base before merging.
- Contract-contained rebase conflicts get one bounded autonomous repair attempt before escalation.

## Phase 3: Repository-Specific Validation and Release Readiness

Goal:

Make the orchestrator reliably follow the target repository's own development workflow, validation requirements, and release policy without baking project-specific infrastructure into `auto_develop`.

Scope:

- Validation profiles.
- Repository instruction ingestion.
- Domain-specific evidence requirements.
- Validation and benchmark evidence when required by the target repo.
- Optional PR automation.

Required capabilities:

1. Define verification profiles by task type:
   - Code-only.
   - Documentation.
   - Benchmark.
   - Domain validation.
   - Release preparation.
2. Load repository instructions and context that define validation, benchmark, remote execution, and release expectations.
3. Require task contracts to state domain assumptions when the target repo's workflow requires them.
4. Detect risky fixture, tolerance, golden-output, or benchmark changes unless explicitly permitted.
5. Record benchmark or validation deltas as evidence when the task or repo instructions require them.
6. Prepare a release branch or pull request candidate with complete review, metrics, budget, and evidence artifacts.

Success criterion:

The system can support a release-sized objective by decomposing it into bounded tasks, following the target repository's documented workflow, and maintaining auditable evidence for each accepted change.

Current implementation status:

- Task contracts support task types for code, documentation, benchmark, validation, and release preparation work. One current enum value still uses legacy domain-specific naming and should be generalized compatibly.
- Verification may reference a named project verification profile instead of repeating commands in each contract.
- Validation and benchmark tasks require explicit assumptions through the existing `scientific_assumptions` field. The field name is now legacy/domain-specific wording and should be generalized in a compatibility-preserving schema cleanup.
- Deterministic review detects unapproved fixture-like and tolerance-like changes. This is useful beyond scientific code and should be documented as generic validation-hardening behavior.
- Evidence bundles persist `scientific_review.yaml`. The artifact name is legacy/domain-specific wording and should be generalized in a compatibility-preserving cleanup.
- Benchmark tasks or benchmark-like file changes persist `benchmark_delta.json`.
- Strong-model planning, release queues, feature-branch integration, task branch cleanup, release review, metrics, budget, and tuning artifacts are implemented.
- PR automation is not implemented.

Remaining Phase 3 work is now small:

1. Rename or alias domain-specific public terms such as `scientific_validation`, `scientific_assumptions`, and `scientific_review.yaml` to generic validation terminology while preserving backward compatibility.
2. Make repository instruction ingestion more explicit so target repos can declare when benchmarks, domain validation, remote commands, or PR policies are required.
3. Add optional PR creation or PR-preparation automation for the final feature branch.

## Phase 4: Autonomous Roadmap Governor

Goal:

Let the governor agent own the upstream development loop: read the roadmap, documentation, repository state, run artifacts, and repository goal; infer the next backlog epics; prioritize them by expected reward; select one epic; decompose it into objectives, contracts, and worker tasks; run the work; review the result; update roadmap/backlog state; and repeat.

Required capabilities:

1. Read roadmap, repo-state memory, recent run summaries, and tuning artifacts.
2. Identify actionable epics and reject duplicate or already-completed work.
3. Prioritize epics against an explicit repository goal.
4. Write a selected epic as a bounded release objective.
5. Generate contracts for that objective through `plan-release`.
6. Run the resulting release through `run-objective` or `run-release`.
7. Update repo-state memory and backlog state after each accepted or failed epic.
8. Continue until budget, explicit stopping criteria, or no actionable epics remain.
9. For validation-heavy or simulation repositories, promote new findings from benchmarks, failed validations, generated artifacts, and changed assumptions into future roadmap/backlog decisions.

Current implementation status:

- `agent-loop plan-backlog --mode strong-model --execute-planner` lets the configured planner agent read bounded documentation, roadmap context, and the repository goal, then emit a validated `BacklogPlan` with prioritized epics and one selected next epic.
- `BacklogPlan` now carries `roadmap_updates` and `repo_state_updates` so the governor can surface learned roadmap/backlog changes from docs, artifacts, metrics, and validation evidence.
- Deterministic `plan-backlog` remains available as fallback/test scaffolding, not as the target autonomous governor behavior.
- Objective-to-contract and contract-to-release execution already exist through `plan-release`, `run-objective`, and `run-release`.

Remaining Phase 4 work:

1. Add a persistent backlog state file so completed, skipped, blocked, and active epics are tracked across runs.
2. Add a higher-level `run-backlog` command that chains `plan-backlog` -> `run-objective` for one selected epic.
3. Teach the governor to generate sub-task contracts directly from the selected epic and decide which workers can run in parallel.
4. Teach the governor to apply or commit policy-compliant roadmap/backlog/repo-state updates after each epic with outcome, metrics, and next recommendations.

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

### Agent Converts Domain Task into Test-Passing Task

Risk:

The agent weakens domain behavior while making tests pass.

Mitigation:

- Forbid validation weakening.
- Require domain assumptions when the target repo workflow calls for them.
- Require benchmark or validation evidence when the target repo workflow calls for it.
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
- Escalate to stronger-model diagnosis first; use human escalation only at configured policy boundaries or after autonomous repair is exhausted.

### Strong Model Rate Limits

Risk:

The orchestration loop stalls if every task requires frontier reasoning.

Mitigation:

- Use strong models only at control points.
- Keep task execution cheap.
- Batch planning or review where possible.
- Cache stable context.

### Alternative Model Backends Add Operational Complexity

Risk:

Serving additional model backends introduces latency, reproducibility, quota, authentication, and quality variance.

Mitigation:

- Exclude open models from Phase 1.
- Later restrict them to low-risk summarization and triage tasks.

### Existing Orchestrator Projects May Partially Overlap

Risk:

Building all worktree or session machinery from scratch may duplicate existing tools.

Mitigation:

- Keep v1 modular.
- Make executor and session runner replaceable.
- Implement project-specific policy separately from generic agent-running.

### Context Compression Removes Important Details

Risk:

Compression can destroy domain assumptions, validation rules, or numerical constraints.

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
Add or improve one documentation-only check without domain behavior changes.
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
