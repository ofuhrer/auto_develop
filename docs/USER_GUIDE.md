# User Guide

This guide explains how to use `auto_develop` as a local orchestration tool for an existing development project. It focuses on the current implemented workflow: bounded task contracts, isolated Git worktrees, deterministic verification, release-level orchestration, feature-branch integration, evidence bundles, and cleanup.

## Mental Model

`auto_develop` runs outside the target project and controls it through Git worktrees.

The usual structure is:

```text
auto_develop/
  main/                    # this orchestrator repository

target_project/
  main/                    # normal target repository checkout
    .auto_develop/
      repo_state/           # tracked durable target memory
      objectives/           # tracked/reviewable selected objectives
      contracts/            # tracked/reviewable task contracts
      runs/                 # ignored or archived raw run evidence

target_project_worktrees/  # temporary agent worktrees for the target project
```

The core objects are:

- Project config: tells `auto_develop` where the target repo is, where to create worktrees, which models to use, and which verification commands are valid.
- Objective: describes a larger feature or release-level goal.
- Task contract: describes one bounded implementation task with allowed files, forbidden changes, required evidence, verification, and stop conditions.
- Release run: executes a queue of task contracts and integrates accepted work into a feature branch.
- Evidence bundle: stores prompts, executor output, verification output, review decisions, diffs, and finalization metadata.

Artifact ownership matters. The `auto_develop` source checkout should be replaceable. For external target projects, store durable target-specific state in the target repository or in a dedicated control repository, not in the `auto_develop` implementation repo. The exception is self-development: when `auto_develop` is the target, its own `repo_state/`, `objectives/`, and `contracts/` are correctly tracked in this repo.

Durable target artifacts:

- `.auto_develop/repo_state/<project>/`;
- `.auto_develop/objectives/`;
- `.auto_develop/contracts/`;
- compact release/epic outcome summaries used by the next backlog-planning run.

Generated/local artifacts:

- `.auto_develop/runs/` raw evidence, unless you intentionally archive it;
- temporary worktrees;
- virtual environments, caches, and raw worker scratch files.

If you delete and reclone only the `auto_develop` controller checkout, you can continue implementing the target repository's next epic only if the target repo or a dedicated control repo contains the durable artifacts above. Raw `runs/` evidence is useful for audit, but should not be the only source of project memory, completed-epic history, or next-epic planning state.

The default Git model is:

```text
main
  └─ feature/<release>
       ├─ agent/<release>/<task-a>
       ├─ agent/<release>/<task-b>
       └─ agent/<release>/<task-c>
```

The orchestrator owns `feature/<release>`. Workers operate on `agent/<release>/<task>` branches in isolated worktrees. Accepted task branches can be merged into the feature branch. The feature branch can then be pushed for review or merged into `main`.

## Step 1: Install The CLI

From the `auto_develop/main` repository:

```bash
uv venv
uv pip install -e ".[dev]"
```

Check that the command is available:

```bash
.venv/bin/agent-loop --help
```

If your environment does not resolve the editable install correctly, use:

```bash
PYTHONPATH=src .venv/bin/python -m agentic_devloop --help
```

For the rest of this guide, replace `agent-loop` with `PYTHONPATH=src .venv/bin/python -m agentic_devloop` if needed.

## Step 2: Prepare The Target Repository

Start from a clean target repository.

```bash
cd /path/to/target_project/main
git status --short --branch
git fetch origin
git switch main
git pull --ff-only origin main
```

The base checkout should be clean unless you explicitly pass `--allow-dirty`. For normal autonomous runs, do not use `--allow-dirty`.

Create a separate worktree root outside the target repo:

```bash
mkdir -p /path/to/target_project_worktrees
```

Do not place the worktree root inside the target repository. This avoids accidental commits of generated worktrees or runtime artifacts.

## Step 3: Create A Project Config

Create `configs/<project>.yaml` in the `auto_develop/main` repository or in a separate operator/control repository. For long-lived target development, prefer storing target-specific durable artifacts in the target repository and passing explicit artifact directories to commands.

Example:

```yaml
project_id: my_project
repo_path: /path/to/target_project/main
default_base_branch: main
worktree_root: /path/to/target_project_worktrees
repo_state_path: .auto_develop/repo_state/my_project

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
    release_preparation: reviewer
  budget_class_roles:
    L: reviewer
    XL: planner
  escalation_role: reviewer

verification_profiles:
  default:
    commands:
      - cd /path/to/target_project/main && PYTHONPATH={worktree}/src .venv/bin/python -m pytest
  code_only:
    commands:
      - cd /path/to/target_project/main && PYTHONPATH={worktree}/src .venv/bin/python -m pytest
  documentation:
    commands:
      - cd /path/to/target_project/main && PYTHONPATH={worktree}/src .venv/bin/python -m pytest

budget:
  max_executor_attempts_per_task: 2
  max_strong_model_calls_per_release: 10
  max_changed_files_per_task: 8
  max_diff_lines_per_task: 600
  max_context_chars_per_task: 30000
```

Validate the config:

```bash
agent-loop config --project my_project --validate-repo
```

Important config fields:

- `project_id`: name used by CLI commands.
- `repo_path`: absolute path to the target repository checkout.
- `default_base_branch`: branch used as the stable base, usually `main`.
- `worktree_root`: absolute path where agent worktrees are created.
- `repo_state_path`: optional project state directory. Relative paths resolve against the orchestrator repo first.
- `executor`: default execution agent.
- `model_catalog`: advisory model policy with capabilities, budget class, and support status.
- `model_roles`: named model roles for workers, reviewers, planners, routers, and repair agents.
- `model_routing`: routing rules from task type or budget class to model role.
- `verification_profiles`: named command sets task contracts can reference.
- `budget`: deterministic limits used during planning and review.

Worktrees normally do not contain their own `.venv`. Prefer verification commands that use a configured shared runtime from the main checkout while pointing imports or source paths at the task worktree. The `{worktree}` placeholder is the intended convention for worktree-aware commands; until every executor path expands it, use explicit absolute paths or wrapper scripts that receive the worktree path from project config. Avoid commands such as `.venv/bin/python` that assume the virtual environment exists inside each isolated worktree.

Recommended durable target directories:

```bash
mkdir -p /path/to/target_project/main/.auto_develop/repo_state/my_project
mkdir -p /path/to/target_project/main/.auto_develop/objectives
mkdir -p /path/to/target_project/main/.auto_develop/contracts
mkdir -p /path/to/target_project/main/.auto_develop/runs
```

Commit `.auto_develop/repo_state`, `.auto_develop/objectives`, and `.auto_develop/contracts` when they define durable development state. Ignore or externally archive `.auto_develop/runs` unless the project deliberately keeps run evidence in Git.

### Model Policy

The recommended hierarchy is:

- `gpt-5.5` for strategic planning, hard debugging, and semantic review.
- `gpt-5.2` for runtime/review supervision where long-session stability matters.
- `gpt-5.3-codex` for implementation workers.
- `gpt-5.3-codex-spark` for tiny repair loops, when supported by the account.
- `gpt-5.4-mini` for cheap routing, summarization, and safe fallbacks.

The current `agent-loop` runtime orchestrator is deterministic Python. It schedules tasks, manages branches, verifies work, and writes evidence. Models are bounded executors or planners behind explicit roles. Because model availability can change by account and over time, mark uncertain catalog entries as `unknown` and keep at least one known supported fallback for worker and repair roles.

## Step 4: Run Doctor

Before planning or launching a governed release, run the preflight diagnostics:

```bash
agent-loop doctor \
  --project my_project \
  --release my-feature-1
```

`doctor` reports repo cleanliness, stale worktrees, existing integration or task branches for the release, and model-routing warnings. If it flags unsupported routing or a size pressure pattern, update `configs/<project>.yaml` before the next run.

## Step 5: Write An Objective

An objective describes a release-sized goal. For self-development, store it under `objectives/`. For external targets, prefer `.auto_develop/objectives/` in the target repository.

Example `objectives/my-feature-1.yaml`:

```yaml
release_id: my-feature-1
title: Add import validation workflow
objective: >
  Add a small import validation workflow that checks input files before they are
  processed and reports actionable validation errors.
non_goals:
  - Do not redesign the full processing pipeline.
  - Do not change production data formats.
acceptance_criteria:
  - Invalid input files are rejected with clear errors.
  - Existing valid inputs still pass.
  - Unit tests cover the new validation behavior.
  - Documentation explains how to use the validation workflow.
```

Use objectives for planning. Use contracts for execution.

The intended workflow is autonomous-first. Humans define the repository goal and hard policy boundaries; the current governor service chooses one epic at a time, produces an objective, and feeds the existing objective/contract/release machinery. The runtime supervisor now records structured repair/resume evidence for recoverable release failures, and the broader multi-epic governor loop plus always-on state refresh remain planned beyond the current one-epic governor boundary.

Use backlog planning first:

```bash
agent-loop plan-backlog \
  --project my_project \
  --goal "Move toward fully autonomous roadmap-driven development" \
  --mode strong-model \
  --execute-planner \
  --write-objective
```

This writes `runs/<timestamp>_<project>_backlog/backlog_plan.json` and, when `--write-objective` is set, an objective YAML for the highest-priority epic. For external targets, pass `--runs-dir /path/to/target_project/main/.auto_develop/runs` and `--objectives-dir /path/to/target_project/main/.auto_develop/objectives`. The backlog plan can also include `roadmap_updates` and `repo_state_updates` that the current governor boundary can consume for follow-up state updates after runs; the broader always-on refresh loop is still planned.

For self-development, `auto_develop` keeps this memory under `repo_state/auto_develop/`, including `architecture_summary.md`, `active_constraints.yaml`, `known_failures.md`, `release_plan.yaml`, and `backlog_state.yaml`. For external targets, use `.auto_develop/repo_state/<project>/` in the target repository or an equivalent durable control repository.

## Step 6: Create Or Generate Task Contracts

A task contract is the unit of worker execution. It must be narrow enough that an agent can complete it autonomously. For external targets, store accepted/generated contracts in `.auto_develop/contracts/` in the target repository or control repository.

Example `contracts/my-feature-0001.yaml`:

```yaml
task_id: my-feature-0001
release_id: my-feature-1
title: Add input validation unit tests
task_type: code_only
budget_class: M

objective: >
  Add focused tests for accepted and rejected input validation cases.

allowed_files:
  - tests/test_input_validation.py
  - src/my_project/input_validation.py

forbidden_changes:
  - Do not change public CLI behavior.
  - Do not update generated outputs.
  - Do not broaden the feature beyond input validation.

required_evidence:
  - git diff
  - test output
  - changed-files list
  - executor summary
  - verifier summary

verification:
  profile: code_only

stop_conditions:
  - More than 2 files changed.
  - More than 250 diff lines.
  - Verification fails twice.
  - The task requires changing files outside allowed_files.

scientific_assumptions:
  - Not applicable; developer tooling task.
```

Contract authoring rules:

- Keep `allowed_files` tight.
- Use `forbidden_changes` to block risky shortcuts.
- Make `verification` deterministic.
- Include explicit `stop_conditions`.
- Split a feature into multiple contracts when implementation, tests, and docs can be isolated.
- Use `depends_on` when one task must wait for another.

Example dependency:

```yaml
depends_on:
  - my-feature-0001
```

## Step 7: Plan A Release

Use deterministic planning to inspect objective coverage and planning artifacts without executing workers:

```bash
agent-loop plan-release \
  --objective objectives/my-feature-1.yaml \
  --project my_project \
  --inspect-proposed-contracts
```

Use strong-model planning when you want the configured planner model to draft contracts:

```bash
agent-loop plan-release \
  --objective objectives/my-feature-1.yaml \
  --project my_project \
  --mode strong-model \
  --execute-planner \
  --inspect-proposed-contracts \
  --write-contracts-dir contracts
```

Review generated contracts before execution. Check that:

- The contracts cover the objective acceptance criteria.
- Each task is bounded and has narrow `allowed_files`.
- Verification profiles exist in the project config.
- The generated queue does not exceed project budgets.
- The feature branch remains a coherent review unit.

## Step 8: Run One Task

Use `run-task` for a single bounded task:

```bash
agent-loop run-task \
  --project my_project \
  --contract contracts/my-feature-0001.yaml
```

By default, `run-task`:

- creates an isolated worktree;
- creates an `agent/<release>/<task>` branch;
- writes an executor prompt;
- runs the configured worker;
- runs verification;
- collects evidence under `runs/`;
- produces an accept, reject, or escalate decision.

It does not commit, merge, or push unless requested.

Useful finalization options:

```bash
agent-loop run-task \
  --project my_project \
  --contract contracts/my-feature-0001.yaml \
  --commit-on-accept
```

```bash
agent-loop run-task \
  --project my_project \
  --contract contracts/my-feature-0001.yaml \
  --merge-on-accept
```

```bash
agent-loop run-task \
  --project my_project \
  --contract contracts/my-feature-0001.yaml \
  --push-on-accept
```

For normal feature work, prefer `run-release` over repeated direct `run-task` calls because release orchestration gives you one coherent feature branch.

## Step 9: Run A Release

Run a release from explicit contracts:

```bash
agent-loop run-release \
  --project my_project \
  --release my-feature-1 \
  --contract contracts/my-feature-0001.yaml \
  --contract contracts/my-feature-0002.yaml \
  --merge-on-accept
```

Run a release from `repo_state/<project>/release_plan.yaml`:

```bash
agent-loop run-release \
  --project my_project \
  --release my-feature-1 \
  --merge-on-accept
```

Sequential execution is the default. Parallel execution currently uses explicit `depends_on` and conservative inferred file-overlap dependencies:

```bash
agent-loop run-release \
  --project my_project \
  --release my-feature-1 \
  --execution-mode parallel \
  --merge-on-accept
```

`run-release` defaults to the integration branch `feature/<release>`. Accepted task branches are merged into that feature branch when `--merge-on-accept` or `--push-on-accept` is set.

Overlap handling is evolving. The current scheduler is intentionally cautious, but the target autonomous workflow is not "no overlap at all." The governor should minimize overlap, classify overlap risk, and decide whether tasks can run in parallel, must be chained, should use stacked branches, or need an explicit merge-repair plan. Deterministic code should only hard-block unsafe overlap such as generated artifacts, lockfiles, migrations, configured exclusive paths, forbidden paths, or files outside task scope.

After the run, inspect `release_metrics.json`, `release_budget.json`, and `release_tuning.md` alongside the release log. The budget ledger records usage, task-size outliers, verification bottlenecks, and waste signals; the tuning report translates those signals into guidance for the next run.

If the report shows routing pressure, reduce task size or adjust `model_routing` and `budget.max_changed_files_per_task`, `budget.max_diff_lines_per_task`, or `budget.max_context_chars_per_task` before launching the next release. Small budget overages should be treated as review findings rather than automatic waste: a reviewer/supervisor agent should decide whether a cohesive verified diff is acceptable, whether to split follow-up work, or whether to rerun with a narrower contract.

Common release modes:

```bash
agent-loop run-release \
  --project my_project \
  --release my-feature-1 \
  --merge-on-accept \
  --release-finalize none
```

```bash
agent-loop run-release \
  --project my_project \
  --release my-feature-1 \
  --merge-on-accept \
  --release-finalize push-feature
```

```bash
agent-loop run-release \
  --project my_project \
  --release my-feature-1 \
  --merge-on-accept \
  --release-finalize merge-main
```

Use `push-feature` when you want a feature branch ready for PR review. Use `merge-main` or `push-main` only when automatic integration into `main` is appropriate for the project.

## Step 10: Monitor A Running Release

At startup, `run-release` prints the release log path:

```text
[agent-loop] Logs: runs/<release-run-id>/release.log (raw: runs/<release-run-id>/release.raw.log)
```

Monitor it in another terminal:

```bash
tail -f runs/<release-run-id>/release.log
```

The filtered log is a human cockpit for live monitoring. It uses color and emojis for quick scanning, reports the current task objective and scope, shows selected models and executor attempts, emits periodic "still working" heartbeats for long-running workers, summarizes useful worker-reported sections, and highlights when a human should inspect, steer, or interrupt. Full raw worker stdout and stderr are retained in:

```text
runs/<release-run-id>/release.raw.log
```

If a release needs repair or resume, also inspect `runs/<release-run-id>/runtime_supervisor/` for structured release events, retry budgets, release-summary references, and repair evidence.

Release outputs normally include:

- `release_summary.json`;
- `release_metrics.json`;
- `release_budget.json`;
- `release_tuning.md`;
- `release_review.md`;
- per-task evidence bundles;
- human-facing `release.log`;
- raw audit `release.raw.log`.

Use `release_metrics.json` for cost and routing analysis. It records per-task prompt size, context size, output size, executor attempts, model usage, verification duration, changed-file count, and diff size. These are character-count proxies, not provider-billed token counts, but they are enough to compare task sizes, model routing, context budgets, and wasted fallback attempts.

## Step 11: Inspect Evidence

After a run, inspect recent summaries:

```bash
agent-loop status --limit 5
```

For a specific run, inspect:

```bash
ls runs/<run-id>/
find runs/<run-id> -maxdepth 4 -type f | sort
```

Typical evidence files include:

- executor prompt;
- executor stdout and stderr;
- executor attempt metadata;
- verification output;
- changed-file list;
- diff;
- failure diagnosis evidence when executor or verification failures exceed bounded retries;
- deterministic review;
- finalization metadata;
- conflict-repair evidence when applicable.

Do not treat an accepted decision as a proof of semantic correctness. It means the task passed configured deterministic/model gates and stayed within the contract. If the repository policy allows autonomous finalization, the system should proceed; otherwise escalate only at the configured policy boundary.

When a task fails, inspect `failure_diagnosis.yaml` first. It records the failure category, confidence, recommendation, and retry or escalation guidance derived from the same evidence bundle.

## Step 12: Finalize Or Review The Feature Branch

If the release used `--merge-on-accept`, inspect the feature branch:

```bash
cd /path/to/target_project/main
git switch feature/my-feature-1
git log --oneline --decorate --graph --max-count=20
git diff main...feature/my-feature-1
```

Push the feature branch if it was not already pushed:

```bash
git push -u origin feature/my-feature-1
```

Then open a PR using your normal project workflow.

If you explicitly want the orchestrator to finalize the release:

```bash
agent-loop run-release \
  --project my_project \
  --release my-feature-1 \
  --merge-on-accept \
  --release-finalize push-feature
```

The finalization choices are:

- `none`: leave the accepted feature branch locally.
- `push-feature`: push `feature/<release>` to `origin`.
- `merge-main`: merge `feature/<release>` into the base branch locally.
- `push-main`: merge `feature/<release>` into the base branch and push it.

## Step 13: Clean Up Artifacts

Normal release runs remove task worktrees and merged task branches unless `--debug-keep-artifacts` is set. Accepted unfinalized work, unmerged accepted branches, and failed-finalization branches are preserved so work remains recoverable.

Dry-run cleanup first:

```bash
agent-loop cleanup \
  --project my_project \
  --release my-feature-1
```

Remove matching task worktrees and `agent/<release>/*` branches:

```bash
agent-loop cleanup \
  --project my_project \
  --release my-feature-1 \
  --force
```

Also delete the integration branch only when it is no longer needed:

```bash
agent-loop cleanup \
  --project my_project \
  --release my-feature-1 \
  --force \
  --include-integration-branch
```

Cleanup refuses to remove paths outside the configured `worktree_root` and refuses to delete the currently checked-out branch.

## Command Reference

### `agent-loop init`

Prints the project and repository values for a new project config.

```bash
agent-loop init --project my_project --repo /path/to/target_project/main
```

This is currently a lightweight helper, not a full config generator.

### `agent-loop config`

Loads and prints a project config.

```bash
agent-loop config --project my_project --validate-repo
```

Key options:

- `--project`: project identifier.
- `--config-dir`: directory containing config YAML files, default `configs`.
- `--validate-repo`: fail if `repo_path` does not exist.

### `agent-loop plan-release`

Creates a contract plan from an objective.

```bash
agent-loop plan-release --objective objectives/my-feature-1.yaml
```

### `agent-loop plan-backlog`

Runs the roadmap-governor agent over repository documentation, roadmap context, and an explicit repository goal, then emits prioritized epics. With `--write-objective`, it writes the highest-priority epic as a release objective that can be passed to `run-objective`.

```bash
agent-loop plan-backlog \
  --project my_project \
  --goal "Move toward fully autonomous roadmap-driven development" \
  --roadmap docs/design/ROADMAP_AND_BACKLOG.md \
  --mode strong-model \
  --execute-planner \
  --write-objective
```

Key options:

- `--project`: project config to use.
- `--goal`: prioritization goal for the backlog planner.
- `--roadmap`: roadmap Markdown file to analyze.
- `--mode strong-model --execute-planner`: have the configured planner agent choose and prioritize epics from the documentation, roadmap, and goal.
- `--write-objective`: write the selected epic into `objectives/`.

Use this command before `plan-release` or `run-objective` when the next development epic should be selected by the roadmap-governor agent instead of manually supplied. Deterministic mode is only a fallback and test scaffold. The current governor path can use run artifacts, validation findings, metrics, and tuning reports to inform the next one-epic cycle; fully automated repeated-cycle refresh is still planned.

Key options:

- `--objective`: release objective YAML file.
- `--mode deterministic`: create conservative planning artifacts without model execution.
- `--mode strong-model`: use strong-model planning mode.
- `--project`: required for strong-model mode.
- `--execute-planner`: call the configured planner backend instead of only writing the planner prompt.
- `--inspect-proposed-contracts`: include generated contract details in CLI output.
- `--write-contracts-dir`: write validated contract drafts without executing them.
- `--contracts-dir`: directory containing existing contracts, default `contracts`.
- `--runs-dir`: directory for planning output, default `runs`.

### `agent-loop run-task`

Runs one task contract in one isolated worktree.

```bash
agent-loop run-task \
  --project my_project \
  --contract contracts/my-feature-0001.yaml
```

Key options:

- `--project`: project identifier.
- `--contract`: task contract YAML file.
- `--verification-timeout-seconds`: timeout for each verification command.
- `--allow-dirty`: allow creating a worktree from a dirty base repository.
- `--commit-on-accept`: commit accepted task changes in the task worktree.
- `--merge-on-accept`: commit and merge accepted task changes into the base branch.
- `--push-on-accept`: commit, merge, and push accepted task changes to origin.
- `--commit-message`: custom commit message for accepted task changes.

### `agent-loop run-release`

Runs a release queue of task contracts.

```bash
agent-loop run-release \
  --project my_project \
  --release my-feature-1 \
  --merge-on-accept
```

Key options:

- `--project`: project identifier.
- `--release`: release identifier.
- `--contract`: explicit contract path. Can be passed multiple times.
- `--execution-mode sequential`: run tasks one after another.
- `--execution-mode parallel`: run ready tasks concurrently using dependency and file-overlap analysis.
- `--integration-branch`: custom feature branch. Defaults to `feature/<release>`.
- `--continue-on-failure`: keep running after a non-accepted task.
- `--debug-keep-artifacts`: preserve task worktrees and branches for debugging.
- `--release-finalize none`: leave feature branch locally.
- `--release-finalize push-feature`: push the feature branch.
- `--release-finalize merge-main`: merge the feature branch into the base branch locally.
- `--release-finalize push-main`: merge the feature branch into the base branch and push it.

Finalization options inherited from `run-task`:

- `--commit-on-accept`: commit accepted task work.
- `--merge-on-accept`: commit and merge accepted task branches into the integration branch.
- `--push-on-accept`: commit, merge, and push accepted task changes.

For release workflows, `--merge-on-accept --release-finalize push-feature` is usually the safest autonomous mode because it creates a reviewable feature branch without pushing directly to `main`.

### `agent-loop run-objective`

Plans contracts from an objective, writes validated contracts, and runs the resulting release.

```bash
agent-loop run-objective \
  --project my_project \
  --objective objectives/my-feature-1.yaml \
  --mode strong-model \
  --execute-planner \
  --merge-on-accept \
  --release-finalize push-feature
```

Key options:

- All relevant `plan-release` planning options.
- All relevant `run-release` execution and finalization options.

This is the highest-level command. Use it only when you trust the planner output and admission checks enough to proceed directly into execution.

### `agent-loop run-backlog`

Selects one epic from backlog planning and executes it through the objective/release orchestration flow.

```bash
agent-loop run-backlog \
  --project my_project \
  --goal "Move toward fully autonomous roadmap-driven development" \
  --mode strong-model \
  --execute-planner \
  --merge-on-accept \
  --release-finalize push-feature
```

Key options:

- `--project`: project identifier.
- `--epic-id`: optional backlog epic identifier to execute. Omit it to run the epic selected by the backlog planner.
- `--goal`: repository goal passed to backlog planning.
- `--roadmap`: roadmap Markdown file to analyze.
- `--mode strong-model`: required execution mode for backlog runs.
- `--execute-planner`: required; executes planner backend instead of prompt-only output.
- All relevant `run-release` execution and finalization options.

Relationship to nearby commands:

- `plan-backlog` selects and prioritizes epics. With `--write-objective`, it stops after writing the selected epic as an objective YAML file.
- `run-backlog` performs backlog planning first, then selects the requested epic or the planner-selected epic, reuses `objectives/<suggested_release_id>.yaml` when it already exists, or writes that objective file when it does not.
- `run-objective` skips backlog selection and starts from an existing objective file.
- `run-backlog` forwards the usual release execution and finalization flags, so `--commit-on-accept`, `--merge-on-accept`, `--push-on-accept`, and `--release-finalize` still control task commits and release finalization.

`run-backlog` is intentionally one epic per invocation. A multi-epic governor command is planned but not yet the documented user workflow.

Run-backlog evidence records artifact paths when produced, including:
- backlog plan (`backlog_plan.json`);
- generated objective path (when run-backlog creates a new objective file);
- contract plan (`contract_plan.json`);
- release summary (`release_summary.json`);
- release metrics (`release_metrics.json`);
- release budget (`release_budget.json`);
- release tuning (`release_tuning.md`).

### `agent-loop status`

Shows recent run summaries.

```bash
agent-loop status --limit 10
```

Key options:

- `--runs-dir`: directory containing run evidence, default `runs`.
- `--limit`: maximum number of runs to show.

### `agent-loop cleanup`

Cleans stale release worktrees and branches.

```bash
agent-loop cleanup --project my_project --release my-feature-1
```

Key options:

- `--project`: project identifier.
- `--release`: release identifier.
- `--force`: actually remove artifacts. Without it, cleanup is a dry run.
- `--include-integration-branch`: also delete `feature/<release>` when it exists and is not checked out.

## Recommended Workflows

### Conservative First Run

Use this when applying `auto_develop` to a new project.

```bash
agent-loop config --project my_project --validate-repo
agent-loop plan-release --objective objectives/my-feature-1.yaml --project my_project
agent-loop run-task --project my_project --contract contracts/my-feature-0001.yaml
agent-loop status --limit 5
```

Review evidence before enabling automatic commits or merges.

### Reviewable Feature Branch

Use this when contracts are bounded and verification is reliable.

```bash
agent-loop run-release \
  --project my_project \
  --release my-feature-1 \
  --execution-mode sequential \
  --merge-on-accept \
  --release-finalize push-feature
```

This leaves `main` stable and produces a reviewable `feature/<release>` branch.

### Parallel Release Run

Use this when tasks are independent or only lightly overlapping.

```bash
agent-loop run-release \
  --project my_project \
  --release my-feature-1 \
  --execution-mode parallel \
  --merge-on-accept \
  --release-finalize push-feature
```

Parallel mode rejects broad file overlap and turns minor overlap into dependencies.

### Debug Run

Use this when diagnosing executor behavior.

```bash
agent-loop run-release \
  --project my_project \
  --release my-feature-1 \
  --debug-keep-artifacts
```

After inspection, clean up:

```bash
agent-loop cleanup --project my_project --release my-feature-1
agent-loop cleanup --project my_project --release my-feature-1 --force
```

## Practical Safety Checklist

Before execution:

- Target repo is clean.
- Target repo is on the intended base branch.
- `worktree_root` is outside the target repo.
- Verification commands work manually.
- Contracts have narrow `allowed_files`.
- Contracts include explicit stop conditions.
- Generated outputs are forbidden unless intentionally part of the task.
- `agent-loop cleanup --project <project> --release <release>` shows no stale artifacts from previous runs.

Before merging to `main`:

- `release_review.md` is reviewed.
- Evidence exists for every accepted task.
- Verification passed on the feature branch.
- Generated runtime artifacts are not committed accidentally.
- The feature branch diff is coherent and limited to the objective.

## Troubleshooting

### `agent-loop: command not found`

Use the venv path:

```bash
.venv/bin/agent-loop --help
```

Or run through Python:

```bash
PYTHONPATH=src .venv/bin/python -m agentic_devloop --help
```

### `ModuleNotFoundError: No module named 'agentic_devloop'`

Reinstall from `auto_develop/main`:

```bash
uv pip install -e ".[dev]"
```

If the editable install still fails, use:

```bash
PYTHONPATH=src .venv/bin/python -m agentic_devloop --help
```

### Release Refuses To Start Because Worktree Root Is Not Empty

Inspect the worktree root and run dry-run cleanup:

```bash
agent-loop cleanup --project my_project --release my-feature-1
```

If the listed artifacts are stale:

```bash
agent-loop cleanup --project my_project --release my-feature-1 --force
```

### Release Refuses To Start Because Task Branches Already Exist

Inspect local branches:

```bash
cd /path/to/target_project/main
git branch --list "agent/my-feature-1/*"
```

If they are stale, use cleanup. If they contain useful accepted work, review or merge them manually before deleting.

### Verification Hangs

Use `--verification-timeout-seconds`:

```bash
agent-loop run-task \
  --project my_project \
  --contract contracts/my-feature-0001.yaml \
  --verification-timeout-seconds 120
```

Also inspect `release.log`, `release.raw.log`, and per-task verification evidence.

### Repeated Failure Diagnosis

When a task fails after bounded executor attempts or verification retries, the orchestrator writes `failure_diagnosis.yaml` next to `executor_attempts.json` in the task evidence bundle.

The default diagnosis path is deterministic. It classifies the failure from the recorded contract metadata, executor attempts, verification results, changed files, and log excerpts, then writes a reproducible recommendation with `guidance.retryable` and `guidance.escalate`.

A model-backed diagnosis backend can be swapped into the same backend seam later, but it should consume the same bounded request and evidence bundle. The difference is review strength, not evidence shape: the deterministic path is reproducible locally, while a model-backed path would provide a stronger interpretive pass over the same artifacts.

Before retrying or escalating a task, inspect:

- `failure_diagnosis.yaml`;
- `executor_attempts.json`;
- `verification.log`;
- `executor_stdout.log`;
- `executor_stderr.log`;
- `changed_files.txt`;
- `git_diff.patch`.

Use the diagnosis guidance to decide the next step:

- `contract_mismatch`: narrow the contract or move changes back into the allowed file set before retrying.
- `verification_failure`: fix the underlying verification failure and rerun the task.
- `timeout`: reduce scope or increase walltime before retrying.
- `model_quota`: retry with a fallback model or after the quota resets.
- `executor_error`: inspect the executor logs before deciding whether to retry.

If `guidance.escalate` is true, stop blind retries and hand the task to stronger-model review or a configured human-escalation boundary.

### Merge Or Rebase Conflicts

The orchestrator can attempt one bounded conflict repair when the conflict is limited to files allowed by the contract. If repair fails, inspect the evidence files:

```bash
find runs/<run-id> -name "conflict_repair.yaml" -o -name "finalization.yaml"
```

Then decide whether to repair manually, narrow the contract, or rerun from a clean branch.

## Current Limits

`auto_develop` is useful for bounded autonomous development today. The active direction is a complete autonomous project governor with runtime supervision: it should choose epics from docs/roadmap/state, decompose them, run workers, verify, repair contract-contained failures, update memory, and continue until configured stopping criteria are reached.

Current important limits:

- The strongest workflow still depends on well-written objectives and contracts.
- Fully dynamic model-driven orchestration is still evolving.
- Pull request creation is not yet automated by the CLI.
- Remote execution adapters, such as cluster or SLURM execution, are still project-specific work.
- Human review is still recommended before merging significant work into `main`.
