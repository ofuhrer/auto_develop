# Agentic Devloop

Agentic Devloop is a local CLI orchestrator for bounded AI development tasks.

The first implementation target is a pragmatic loop that can:

1. Load project and task definitions.
2. Create isolated Git worktrees.
3. Run a bounded coding agent.
4. Run deterministic verification.
5. Collect evidence.
6. Produce an accept, reject, or escalate decision.

See [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) and [docs/design](docs/design) for the design documents.

## Development

Install locally:

```bash
uv venv
uv pip install ".[dev]"
```

For editable development installs in environments that ignore editable `.pth` path entries, use:

```bash
PYTHONPATH=src uv run agent-loop --help
```

Run tests:

```bash
pytest
```

Check the CLI:

```bash
agent-loop --help
agent-loop --version
```

Run the local CLI smoke checks:

```bash
PYTHONPATH=src uv run agent-loop --help
PYTHONPATH=src uv run agent-loop --version
PYTHONPATH=src uv run agent-loop config --project rust_rockfall
```

Planner-backend smoke checks:

```bash
PYTHONPATH=src uv run agent-loop plan-release --objective objectives/v0.8.0.yaml
PYTHONPATH=src uv run agent-loop plan-release \
  --objective objectives/v0.8.0.yaml \
  --mode strong-model \
  --project auto_develop
PYTHONPATH=src uv run agent-loop status --limit 5
```

Load a project config:

```bash
agent-loop config --project rust_rockfall
```

Run one bounded task:

```bash
agent-loop run-task \
  --project rust_rockfall \
  --contract contracts/rr-0001.yaml
```

`run-task` creates an isolated worktree, writes an executor prompt, runs the configured executor, runs the contract verification commands, collects an evidence bundle, and writes `decision.yaml`.

Run an ordered release task queue:

```bash
agent-loop run-release \
  --project auto_develop \
  --release sprint-0
```

`run-release` executes existing task contracts in order. If no `--contract` arguments are provided, it reads `current_tasks` from `repo_state/<project>/release_plan.yaml` and maps each task ID to `contracts/<task-id>.yaml`. It stops after the first non-accepted task by default and writes `release_summary.json` under `runs/`.

Before running tasks, `run-release` classifies overlapping `allowed_files` patterns. Minor overlap is allowed in sequential mode, broad overlap blocks parallel mode, and exact same concrete-file overlap is rejected.

Create a conservative release contract plan:

```bash
agent-loop plan-release \
  --objective objectives/v0.8.0.yaml
```

`plan-release` validates whether a release objective has matching contracts and writes `contract_plan.json` under `runs/`. It is deterministic scaffolding; strong-model contract generation is still a future planning mode.

To reserve strong-model planning budget and write the planner prompt artifact:

```bash
agent-loop plan-release \
  --objective objectives/v0.8.0.yaml \
  --mode strong-model \
  --project auto_develop
```

Reviewing generated planning artifacts is a manual step:

1. Inspect `contract_plan.json` for objective coverage, missing tasks, and scope drift.
2. If `--mode strong-model` was used, inspect `planner_prompt.md` for the draft inputs that were sent to the planner.
3. Approve the draft only when the proposed release queue stays within the contract and the follow-up task contracts remain bounded.
4. Promote the approved plan into explicit task contracts before running `run-release`.

To provide an explicit queue:

```bash
agent-loop run-release \
  --project auto_develop \
  --release sprint-0 \
  --contract contracts/ad-0001.yaml
```

To let an accepted task complete the Git path automatically:

```bash
agent-loop run-task \
  --project auto_develop \
  --contract contracts/ad-0001.yaml \
  --push-on-accept
```

`--push-on-accept` commits accepted changes in the task worktree, merges the task branch into the configured base branch, and pushes the base branch to `origin`. Use `--merge-on-accept` to commit and merge without pushing, or `--commit-on-accept` to only commit in the task worktree.

Repo-specific context can be stored under `repo_state/<project>/` and referenced with `repo_state_path` in the project config. `run-task` injects selected state into executor prompts and writes `model_call_metadata.json` into the evidence bundle.

Scientific and benchmark contracts can set `task_type`, use named verification profiles, and declare fixture/tolerance permissions. Phase 3 evidence includes `scientific_review.yaml`, optional `benchmark_delta.json`, and optional `remote_dispatch.yaml`.

Project configs may define `model_roles` and `model_routing` so low-risk tasks use cheap workers while large or release-preparation tasks route to stronger models. Executor roles can also define `fallback_models`; retries and fallback attempts are persisted in `executor_attempts.json`, and executor failures write `failure_diagnosis.yaml`.

Accepted-task finalization uses a local `.git/agent-main.lock`, rebases the task worktree onto the latest base branch available locally or through `origin/<base>`, then merges and pushes when requested.

If a rebase conflict is limited to files allowed by the task contract, the orchestrator writes a bounded conflict-repair prompt, runs one repair worker attempt, reruns verification, and retries finalization once. Unresolved conflicts are escalated with `conflict_repair.yaml` and `finalization.yaml` evidence.

Show recent run summaries:

```bash
agent-loop status --limit 5
```

Before running against `rust_rockfall`, update [configs/rust_rockfall.yaml](configs/rust_rockfall.yaml) so `repo_path` and `worktree_root` point to real local paths.
