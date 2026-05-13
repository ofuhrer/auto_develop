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
- Release queues classify allowed-file overlap. Current scheduling remains conservative, but the target design treats overlap as a risk signal for the governor-owned execution DAG: normal source-file overlap can be parallelized, sequenced, stacked, or assigned a merge-repair plan by a high-level agent; hard deterministic rejection should be reserved for configured exclusive paths, generated artifacts, lockfiles, migrations, and out-of-scope files.
- Project configs support `model_roles`, `model_routing`, and `model_catalog` for capability-aware routing. Default configs express the recommended hierarchy of `gpt-5.5` strategic planning, `gpt-5.2` runtime/review supervision, `gpt-5.3-codex` coding work, `gpt-5.3-codex-spark` micro repair, and `gpt-5.4-mini` cheap routing/fallback. `doctor` reports unsupported, unknown, and uncataloged role models so routing can degrade safely.
- Executor roles support `fallback_models`; attempts are bounded by `budget.max_executor_attempts_per_task`.
- Repeated-failure diagnosis writes `executor_attempts.json` and `failure_diagnosis.yaml` after bounded executor or verification failures; the default backend is deterministic and the diagnosis step is exposed through a replaceable seam for stronger review.
- `agent-loop plan-release` writes deterministic contract planning scaffolds from release objectives and can execute a configured planner backend with `--execute-planner`, preserving planner stdout/stderr/metadata paths in the contract plan.
- `agent-loop run-objective` plans from an objective, writes validated generated contracts, and runs those contracts as a release queue.
- `agent-loop plan-backlog` analyzes the roadmap against a repository goal, emits prioritized epics, and can write the highest-priority epic as a release objective for `run-objective`.
- Generated-contract admission rejects hard safety violations such as release mismatch, missing diff evidence, weak stop conditions, whole-repo scope, unknown verification profiles, and inconsistent verification profiles. Allowed-file counts and diff-size pressure are budget findings; severe violations remain hard stops, while modest overages should be reviewed by the governor/supervisor agent with an auditable accept, split, rerun, or escalation decision.
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

Let the governor agent own the upstream development loop and let a runtime supervisor own recovery while the loop is running. The governor performs a state-review pass over roadmap, documentation, repository state, source/branch state, recent run artifacts, release reviews, metrics, tuning reports, and the repository goal; infers the next backlog epics; prioritizes them by expected reward; selects one or more epics; chooses the execution strategy for each epic; runs the work; triggers an independent feature-review agent when configured; routes reviewer findings to repair agents or backlog follow-up; updates roadmap/backlog state; and repeats. The runtime supervisor observes release events and evidence, diagnoses recoverable failures, applies bounded repairs, normalizes useful-but-invalid model output, and resumes execution without routine human intervention.

Target operator experience:

1. Freshly clone `auto_develop` and the target repository.
2. Give the coding agent one or two onboarding prompts that identify both repositories, the target repo's goal, and hard safety/policy boundaries.
3. Run one high-level command that says, effectively, "implement the next N highest-value epics."
4. Watch the human-facing release log and intervene only for major problems.
5. Receive a clean feature branch, pushed branch, PR candidate, or policy-approved merge, with evidence and updated repo-state memory.

The system should not stop for routine worker or subsystem failures. A high-level runtime supervisor should diagnose, repair, and retry contract-contained failures such as stale editable installs, schema-invalid planner output, missing worktree import context, long-running but active workers, flaky tests, dataclass ordering mistakes, worker verification-environment confusion, over-broad documentation tasks, allowed-file overlap, soft budget overruns, and narrow merge conflicts. Human escalation is reserved for exhausted autonomous repair, missing credentials, unsafe policy expansion, destructive operations not explicitly delegated, hard invariant violations, or no actionable work remaining.

Required capabilities:

1. Read roadmap, repo-state memory, recent run summaries, and tuning artifacts.
2. Inspect live repository state before epic selection, including current branch, dirty state, open feature/agent branches, source layout drift, changed docs, recent release artifacts, and unresolved findings.
3. Identify actionable epics and reject duplicate or already-completed work.
4. Prioritize epics against an explicit repository goal.
5. Write a selected epic as a bounded release objective.
6. Choose an execution strategy: one-shot high-capability implementation, sequential contracts, parallel contracts, stacked branches, patch handoff, or replanning.
7. Generate contracts only when the selected strategy needs decomposition; otherwise produce a one-shot implementation prompt with equivalent objective, scope, verification, and reporting requirements.
8. Run the selected strategy through the deterministic kernel so Git isolation, evidence, verification, hard gates, and finalization policy remain consistent.
9. Review the integrated feature branch with an independent reviewer agent when configured.
10. Normalize useful reviewer output before strict schema validation, then dispatch bounded repair agents, accept risks with rationale, defer findings to backlog, or stop by policy.
11. Update repo-state memory and backlog state after each accepted or failed epic.
12. Continue until the requested epic count, budget, explicit stopping criteria, or no actionable epics remain.
13. For validation-heavy or simulation repositories, promote new findings from benchmarks, failed validations, generated artifacts, and changed assumptions into future roadmap/backlog decisions.
14. Diagnose and repair failed subsystem steps before stopping, including planner schema mismatches, reviewer schema mismatches, verification-environment drift, flaky tests, and small integration conflicts.
15. Observe long-running workers through heartbeats, process liveness, raw audit streams, and worktree diff/file activity; classify active, quiet-alive, stalled, hung, or environment-blocked execution before deciding whether to wait, inspect, interrupt, retry, or escalate.
16. Repair failed release plans by normalizing contracts, splitting genuinely over-budget tasks, accepting cohesive verified soft-over-budget work with evidence when appropriate, narrowing unsafe allowed-file overlap, and resuming from previously accepted work.
17. Decide the execution DAG dynamically: run low-risk tasks in parallel, serialize or stack dependent work, allow normal source overlap when the expected reward exceeds merge risk, and create merge-repair tasks for manageable conflicts.

Current implementation status:

- The governor-control path now has an explicit one-epic `GovernorLoop` service boundary, a typed `StateStore` persistence seam, a `RepairPolicy` decision seam, and typed supervisor decision records for auditable repair/scheduling choices.
- The one-epic execution-strategy seam is implemented in the planning path: `plan-release` and `run-objective` choose one-shot, sequential contracts, parallel contracts, stacked branches, patch handoff, replanning, or stop before contract generation. They always persist `execution_strategy_selection.json`; executable/replan actions also persist `supervisor_decisions/execution_strategy__<decision-id>.json`; `stop` currently has selection JSON only. `one_shot` currently stops after writing the bounded execution input; the worker runner that consumes that input is the next missing execution capability.
- `agent-loop plan-backlog --mode strong-model --execute-planner` lets the configured planner agent read bounded documentation, roadmap context, and the repository goal, then emit a validated `BacklogPlan` with prioritized epics and one selected next epic.
- `BacklogPlan` now carries `roadmap_updates` and `repo_state_updates` so the governor can surface learned roadmap/backlog changes from docs, artifacts, metrics, and validation evidence.
- Deterministic `plan-backlog` remains available as fallback/test scaffolding, not as the target autonomous governor behavior.
- Objective-to-contract and contract-to-release execution already exist through `plan-release`, `run-objective`, and `run-release`.
- `agent-loop run-backlog` chains backlog planning, selected-epic objective creation or reuse, objective-to-contract planning, and release execution for one selected epic.
- `agent-loop run-governor --epic-count N` now composes repeated one-epic cycles with parent governor log/events artifacts, repo-state completed-epic updates, recent release-summary references, and default stops for planning-only or non-accepted cycles.
- Release continuation now recognizes previously accepted and merged tasks for the same release from prior `release_summary.json` artifacts, enabling step-by-step reruns.
- `run-backlog` records artifact paths for backlog plans, generated objectives, contract plans, release summaries, metrics, budgets, tuning reports, and an evidence manifest.
- Planner-output normalization now repairs bounded wrapper/schema drift, missing evidence, stop-condition wording gaps, and worktree-local verification runtime assumptions before generated-contract admission stops when task meaning is unchanged.
- `run-backlog` writes governor-level log artifacts for a one-epic invocation. `run-governor` writes a parent governor log/events stream for repeated cycles, but the cockpit still needs richer live repair/finalization/state-refresh detail before unattended long runs are comfortable.
- Release planning now has deterministic state-review snapshot capture (`state_review_snapshot.json`) plus contract-plan schema support for `state_review_snapshot_path`; full agent-driven pre-epic state-review decisioning is still pending.
- The `governor-service-boundaries` dogfood run showed the exact missing layer: the deterministic kernel correctly caught environment drift, invalid generated contracts, unsafe overlap, long-running worker ambiguity, and over-budget documentation scope, but a human had to act as runtime supervisor to repair and resume.
- The first full closed-loop run showed that the system can recover from subsystem failures, but recovery still required manual patches for planner schema mismatch, verification-environment drift, and one worker-generated dataclass ordering error.
- The runtime-supervisor dogfood run showed that deterministic hard stops are sometimes too blunt: a verified cohesive diff only 19 lines over a 600-line budget should usually become a soft review finding, not automatic wasted work; strict exact-overlap rejection should become governor DAG judgment; worktree verification must use a configured shared runtime instead of assuming `.venv` exists in each worktree.
- The controller/target split needs tightening. Self-development can track `repo_state`, objectives, and contracts in the `auto_develop` repo because it is also the target. External targets should keep durable state, objectives, and contracts in the target repo or a dedicated control repo. Raw `runs/` evidence is local/archival and must not be the only source of development memory, completed-epic history, or next-epic planning state.
- The soft-gates dogfood run showed a planner-output normalization gap: the generated contracts were semantically usable but missed required `git diff`/changed-files evidence and used worktree-local `.venv` verification commands. Fully autonomous execution should repair those bounded defects, rerun deterministic contract admission, and continue without manual materialization.
- The same run showed an observability gap for multi-epic operation: per-release `release.log` files are useful child artifacts, but operators need one top-level `governor.log` to watch backlog planning, objective/contract generation, contract normalization, release execution, repair, finalization, state refresh, and next-epic selection across the whole run.
- The agentic-feature-review-loop run implemented the release-local semantic review boundary: when `model_roles.reviewer` is configured, `run-release` reviews the integrated feature branch, persists `feature_review.json` and `feature_review_recheck.json`, creates bounded repair contracts for required findings, reruns verification, and gates finalization on unresolved required findings or accepted-with-rationale findings. This is a release-local safety boundary, not the broader multi-epic governor; persistent memory, pre-epic state-review decisioning, and N-epic orchestration remain planned.
- The persistent-governor-memory dogfood run showed that useful generated contracts can be blocked by deterministic overlap policy even when a high-level supervisor would simply serialize, stack, or re-slice the work. It also showed reviewer churn: multiple useful repair passes can improve quality, but without explicit convergence semantics the reviewer can keep discovering adjacent issues until retry budget exhaustion. These are not reasons to remove safety; they are reasons to move scheduling, repair-loop continuation, and finding deferral into typed supervisor decisions that rerun deterministic validators.
- Typed supervisor decision records are the immediate implementation seam for those choices; the broader N-epic governor loop that would use them repeatedly remains planned until the state-review and memory layers are fully reliable.
- The `supervisor-owned-release-scheduling` comparison experiment showed that a one-shot high-capability agent can outperform a five-contract multi-agent release on cohesive architectural work. The generated release completed all five tasks but blocked finalization because useful reviewer output failed strict schema validation; the one-shot branch produced a more coherent implementation and passed the same full test suite with less orchestration overhead. This changes the architectural priority: the governor must choose execution strategy before decomposing, and raw planner/reviewer outputs must be normalized before strict typed validation.
- The `review-loop-convergence-policy` dogfood run closed one release-local brittleness point but exposed the remaining multi-epic fragility: final review repair still required a human to interpret reviewer findings, patch evidence semantics, rerun full verification, merge to `main`, and decide whether to delete feature branches. The next governor increment must make "review found repairable issues" an autonomous continuation state, not a stop point.
- The last cycles also showed that successful child releases are not enough for unattended operation. A top-level governor must own the whole sequence: preflight cleanliness, stale branch/worktree cleanup by policy, selected epic execution, final feature review, bounded repair/re-review, merge or PR creation, branch deletion, state-memory compaction, and the next epic selection.
- Review artifacts must become first-class state inputs. `feature_review.json`, `feature_review_recheck.json`, supervisor decision artifacts, release metrics, and tuning reports should feed the next governor decision automatically; otherwise the system depends on a human reading logs and translating lessons into backlog changes.

Prioritized Phase 4 epics:

Completed: `governor-state-review` added deterministic pre-epic state-review snapshot capture and backlog-planning evidence linkage. `agentic-feature-review-loop` added the release-local semantic review gate with structured reviewer artifacts, bounded repair contracts, verification reruns, and finalization gating. `persistent-governor-memory` added durable backlog/repo-state memory seams. `supervisor-decision-records` added typed, auditable supervisor decision records and the first soft-budget runtime consumer. `supervisor-owned-release-scheduling` is implemented for normal source overlap, with follow-up schema/review hardening still useful before broader rollout. `supervisor-execution-strategy` is implemented in the one-epic planning path as the shipped strategy seam. `model-output-normalization` is implemented for bounded planner, reviewer, and supervisor artifact normalization with strict validator reruns and refusal boundaries. `review-loop-convergence-policy` is implemented for release-local duplicate, soft-finding, false-positive, scope-expansion, and backlog-follow-up classification. Historical completed objectives, contracts, and raw runs are no longer retained as active planning inputs in the controller repo.

1. `multi-epic-run-governor-hardening`: harden the initial repeated-cycle `run-governor` shell. The command now accepts an epic count and runs repeated one-epic cycles; remaining work is richer state refresh before each cycle, no-actionable-work detection, stronger stop taxonomy, final-review continuation, and policy-driven finalization/cleanup before claiming unattended N-epic operation.
2. `review-repair-continuation`: make final feature-review findings an autonomous continuation state. The supervisor should normalize useful reviewer output, classify findings, generate bounded repair contracts or one-shot repair prompts, rerun verification and reviewer checks, and only stop when repair budget, hard gates, policy, or credentials require it.
3. `autonomous-finalization-and-cleanup`: after a release-local reviewer accepts the integrated branch, autonomously merge or prepare a PR according to project policy, push the result, delete merged feature/agent branches locally and remotely, prune worktrees, and record the finalization decision as durable state.
4. `governor-cockpit-v2`: expand the governor run/log stream (`governor.log`, `governor.raw.log`, `events.jsonl`) so one tail command monitors the full N-epic run and links state-review snapshots, child release logs, feature reviews, repair loops, state updates, and next-epic selection.
5. `shared-verification-runtime`: add shared verification-runtime configuration so isolated worktrees can run tests through a known Python/toolchain without requiring a per-worktree `.venv`.
6. `environment-repair-actions`: add environment repair for missing `.venv`, command-not-found, missing `PYTHONPATH`, and dependency-runtime drift when project policy declares a safe repair.
7. `executor-liveness-supervision`: improve executor liveness detection with process, output, heartbeat, and file/diff activity signals before declaring a worker stuck.
8. `target-artifact-ownership`: add target-artifact ownership support with project-configured artifact directories, target-repo `.auto_develop/` layout, and compact release/epic outcome summaries in tracked repo-state so deleting the controller checkout does not lose target development memory or next-epic context.
9. `governor-state-refresh`: teach the governor to apply or commit policy-compliant roadmap/backlog/repo-state updates after each epic with outcome, metrics, lessons, review findings, and next recommendations.
10. `onboarding-bootstrap`: add a bootstrap/onboarding command or checklist that turns a freshly cloned target repo plus one or two prompts into the required config, repo-state memory, objective/backlog directories, and initial doctor checks.

Current priority rationale:

1. `multi-epic-run-governor` is now the central product gap. State review, persistent memory, supervisor decisions, release scheduling, execution-strategy selection, model-output normalization, and review-loop convergence are in place; the remaining work is composing them into repeated autonomous epic cycles with state refresh, finalization, cleanup, and stop criteria.
2. `review-repair-continuation` and `autonomous-finalization-and-cleanup` are the immediate brittleness reducers from the last two cycles. Without them, successful releases still stop for human review interpretation, merge/push/branch cleanup, and manual backlog-state updates.
3. `governor-cockpit-v2`, shared runtime, environment repair, liveness, artifact ownership, state refresh, and onboarding then harden operations for repeated unattended runs.

Important dependency order:

1. Implement state review and feature review before N-epic looping.
2. Implement persistent memory before relying on repeated autonomous cycles.
3. Keep the shipped one-epic execution-strategy seam aligned with normalization and review convergence before expanding N-epic looping.
4. Implement review repair continuation and autonomous finalization before claiming multi-epic unattended operation; otherwise the governor can start epics but still needs a human to land them.
5. Add shared verification runtime and environment repair close behind, because repeated worktree test failures are a known source of unnecessary stops.
6. Expand the cockpit log as soon as multiple child artifacts exist, otherwise operators cannot monitor long runs coherently.

Architecture consolidation needed before this should grow much further:

1. Split `release.py` into release coordination, scheduling, reporting, finalization, metrics, and dependency analysis.
2. Split `orchestrator.py` into task execution, evidence/review, model routing, finalization, and repair.
3. Move backlog planning, objective handoff, and multi-epic governor control into separate services instead of growing `backlog.py`.
4. Move CLI backend construction into application-service factories.
5. Split schema models by domain so config, contracts, runtime state, evidence, and governor state can evolve independently.
6. Retire procedural heuristic modules once supervisor-backed decisions exist; keep them only as deterministic test fixtures or fallback scaffolding. Priority candidates are exact-overlap rejection, hard budget rejection for small overages, brittle verification-command assumptions, elapsed-time-only stuck-worker interpretation, and controller-relative target artifact defaults.
7. Track code-reduction explicitly: new supervisor authority should shrink procedural judgment in `release.py`, `planning.py`, `failure_diagnosis.py`, and `budget.py`, not add parallel policy engines beside them.

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
- External-target smoke testing should use target-local `.auto_develop/` config,
  state, contracts, and run directories rather than committing target-specific
  artifacts to this controller repository.

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
- Sample YAML validation uses the current `auto_develop` self-development config;
  historical release contracts are not retained after their release lands.

### Task 0.3: Implement Project Config Loader

Add config support for external target repositories without committing target-specific config to this controller repo.

Acceptance:

- CLI can load and print normalized project config.
- Repo path validation works.
- Missing repo fails safely.

Implemented:

- `agent-loop config --project auto_develop`
- External target configs load correctly when supplied via the target repo or a dedicated control repo.

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
  --project auto_develop \
  --contract contracts/<task-id>.yaml
```

Before using this against an external repository, create the project config and durable state under the target repo's `.auto_develop/` tree or a dedicated control repo.

### Task 0.9: Run Synthetic External-Target Task

Use a low-risk task against an external target repository with target-local config/state/contracts.

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
