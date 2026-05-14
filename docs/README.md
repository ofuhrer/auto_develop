# Documentation

This directory contains user, maintainer, and design documentation for `agentic-devloop`.

## Start Here

- [User Guide](USER_GUIDE.md): step-by-step instructions for using `auto_develop` on an existing development project.
- [Development Guide](DEVELOPMENT.md): setup, test, smoke-test, and documentation practices for contributors.
- [Design Documentation](design/README.md): architecture, technical specification, roadmap, and design risks.

## Project Summary

`agentic-devloop` is an autonomous-first orchestration layer around coding agents. The current governor path is split into a one-epic `GovernorLoop` service, a shipped repeated-cycle `run-governor --epic-count N` command, parent `governor.log`/`events.jsonl` artifacts, completed-epic tracking, recent release-summary recording, a typed `StateStore` persistence seam, a `RepairPolicy` decision seam, typed supervisor decision records for repair/scheduling/execution-strategy/finding adjudication, a shared verification-runtime policy for isolated worktrees, a release-local feature-review/repair/convergence loop, implemented runtime-supervisor repair/resume seams for structured release failures, and cost-runtime governance that reads prior `release_metrics.json` and `release_tuning.md` evidence to choose decomposed, one-shot, or review-capped routing for the next release. That shipped review loop now exposes the reviewer prompt/input, raw reviewer stdout/stderr/metadata, the review recheck, the final review-continuation decision, final integration verification evidence, generated repair contracts, backlog follow-up proposal paths, and typed `final_review_finding_adjudication` records for the final release-local classification step. Final review stop hardening makes the convergence boundary explicit: final integration verification reruns before final adjudication, blocker findings stay blocked, soft and false-positive or verification-only findings may continue only with rationale, duplicate findings are deferred, and scope-expansion or backlog-follow-up findings are written into repo-state follow-up rather than being treated as accepted work. Those seams support roadmap/backlog analysis, state persistence, bounded repair decisions, planner-output normalization, generated-contract admission repair with validator reruns, governor-level logging, semantic review of one integrated feature branch, deterministic state-review snapshot capture (`state_review_snapshot.json`) with contract-plan plumbing through `state_review_snapshot_path`, and a split refresh trail: pre-selection refresh writes `state_refresh_summary.json` alongside the snapshot, while post-cycle refresh writes `post_cycle_state_refresh.json`; either step can emit `state_refresh_error.json` and stop the next selection step. The refresh path records accepted/finalized, failed, blocked, and manual-merge outcomes, and it keeps the compact durable state in `active_epics`, `reviewed_epics`, `completed_epic_records`, `blocked_epic_records`, `recent_run_summaries`, and the finalization outcome references stored by `StateStore`. The target architecture is now explicitly a deterministic kernel plus a high-level governor/supervisor agent: deterministic code owns hard gates, Git/worktrees, verification, evidence, metrics, typed persistence, and finalization mechanics; the supervisor owns judgment-heavy choices such as one-shot versus decomposed execution, planner-output normalization, admission repair, finding adjudication, retry/split/replan decisions, and roadmap updates. The repeated-cycle shell is shipped; the implemented soft scope-risk policy now classifies changed-file and diff-size overages as supervisor-adjudicated findings only after hard gates pass, while broader pre-epic state-review decisioning from live repository evidence, one-shot worker execution, and policy-driven branch cleanup/finalization remain planned.

The project prioritizes:

1. Fast, bounded, pragmatic development loops.
2. Autonomous-first planning and execution inside explicit contracts.
3. Deterministic hard invariants before trust, with agent-governed judgment for soft findings.
4. Auditable evidence for every accepted change.
5. Safe Git integration through isolated worktrees and feature branches.
6. Continuous roadmap/backlog refresh from artifacts, validation evidence, metrics, and domain learnings.
7. Autonomous runtime supervision for contract-contained failures.
8. Domain conservatism for validation, benchmarks, fixtures, and tolerances.

## Current Implementation

Implemented capabilities include:

- project config and task contract schema validation;
- isolated Git worktree creation;
- shared verification-runtime configuration for isolated worktrees;
- Codex CLI executor wrapper with bounded attempts and fallback models;
- `doctor` preflight diagnostics for repo cleanliness, stale worktrees, release-branch collisions, and routing warnings;
- one-epic `GovernorLoop` service boundaries for selected-epic execution and follow-up state refresh inputs;
- typed `StateStore` persistence over repo-state files and run summaries;
- `RepairPolicy` decision seams for classifying failures into retry or stop decisions;
- runtime-supervisor inputs for release events, release summaries, evidence bundles, raw logs, budget ledgers, tuning reports, and backlog-state references;
- cost-runtime governance inputs and outputs: prior `release_metrics.json`, `release_tuning.md`, and typed `cost_runtime_governance__<release-id>.json` decision artifacts;
- bounded repair actions for environment repair, planner-contract normalization, task split or scope narrowing, release resume, long-running-worker inspection, model escalation, and repo-state update proposals;
- structured stop evidence when a hard gate, unavailable model, invalid release-resume request, or exhausted retry budget blocks repair;
- deterministic verification command execution;
- evidence bundle collection;
- deterministic review findings and persisted decisions;
- repeated-failure diagnosis evidence (`executor_attempts.json` and `failure_diagnosis.yaml`) with deterministic classification by default and a replaceable backend seam for stronger review;
- role-based model routing;
- repo-state context injection;
- `run-task`, `run-release`, `plan-backlog`, `plan-release`, `run-objective`, `run-governor`, `status`, and `cleanup` commands;
- `run-backlog` for chaining backlog planning into objective and release execution;
- shared verification-runtime guidance so isolated worktrees can reuse one configured Python/toolchain instead of per-worktree `.venv` directories;
- cost-runtime governance over local release metrics and tuning evidence, with conservative decomposed fallback when metrics are absent or unreadable and hard gates still authoritative;
- supervisor-owned execution-strategy selection for one-epic releases, including `one_shot`, `sequential_contracts`, `parallel_contracts`, `stacked_branches`, `patch_handoff`, `replan`, and `stop` outcomes;
- one-shot planning/input materialization: `one_shot` currently writes a bounded `one_shot_execution_input.json` and returns `release: null`; the one-shot worker execution path is still planned, so `run-backlog` keeps executable decomposition as its default until that runner exists;
- `stop` strategy recording in `execution_strategy_selection.json`; a typed `execution_strategy` supervisor decision artifact is written for executable/replan actions today, while typed blocked-decision persistence for `stop` remains planned;
- bounded planner-output normalization and generated-contract admission repair with strict revalidation, validator-rerun metadata, and hard non-bypassable stops for unsafe release IDs, forbidden/generated-artifact/lockfile/migration changes, configured exclusive paths, out-of-scope files, missing hard evidence, unsafe finalization, failed verification, weak stop conditions, and whole-repo scope; reviewer- and supervisor-output normalization remain planned extensions on the same seam;
- release-local final adjudication after bounded review repair, including typed `final_review_finding_adjudication` records, final integration verification before classification, and repo-state compaction of accepted or deferred findings for the next planning cycle;
- implemented soft scope-risk findings for changed-file and diff-size overages when the hard gates pass, with supervisor adjudication required before the release can continue;
- typed supervisor decision records under `runs/<run-id>/**/supervisor_decisions/` for auditable scheduling, repair, normalization, and soft-budget decisions;
- governor-level log artifacts for `run-backlog` invocations;
- release-level feature branch integration through `feature/<release>`;
- dynamic release DAG scheduling from `depends_on` plus supervisor-owned overlap-risk decisions, with release execution consuming typed scheduling artifacts while hard gates remain authoritative for unsafe scopes;
- soft-gate decision artifacts for accepted exceptions, including task-level `soft_gate_decision.json` and release-level `soft_gate_decisions.json`, each carrying the finding, severity, risk, evidence paths, recommended actions, decision, rationale, fallback plan, and validators to rerun;
- structured `runtime_supervisor/` evidence during repair/resume runs, including release events, retry budgets, repair evidence, and release-summary references;
- human-cockpit `release.log`, raw audit `release.raw.log`, `release_metrics.json`, `release_budget.json`, and `release_tuning.md`;
- default cleanup of merged task worktrees and branches;
- dry-run-first manual cleanup;
- one bounded conflict-repair attempt for contract-contained rebase conflicts.

Important remaining gaps:

- the shipped repeated-cycle governor loop, which still needs hardening for unattended multi-epic operation;
- the shipped one-epic execution-strategy seam, with the remaining gaps being one-shot worker execution and a broader N-epic governor that can consume it across repeated cycles;
- full pre-epic repository state-review decision pass over source state, branches, docs, repo-state memory, recent runs, metrics, and artifacts before selecting the next epic;
- multi-epic orchestration of the implemented release-local feature-review/repair loop;
- richer top-level governor cockpit for monitoring repair, final-review continuation, finalization cleanup, and state refresh across repeated cycles;
- always-on state refresh across repeated epic cycles;
- reduction of deterministic heuristic code once supervisor-backed decisions are available; candidate areas include backlog scoring, contract-normalization heuristics, failure classification, budget-tuning prose, exact-overlap rejection, brittle verification-command assumptions, and cockpit-summary filtering;
- shared verification-runtime policy so isolated worktrees can run tests without per-worktree virtual environments;
- executor liveness classification from process, output, heartbeat, and file/diff activity rather than elapsed time alone;
- target-artifact ownership: external targets should keep durable `.auto_develop/repo_state`, objectives, contracts, and compact outcome history in the target repo or a dedicated control repo, not in the `auto_develop` source checkout;
- policy-driven final branch cleanup across repeated epics remains planned;
- supervisor-owned scheduling decisions are implemented for normal source overlap, but stacked branch re-slicing and a broader N-epic governor loop remain planned;
- cost-runtime governance is a local routing heuristic over release metrics and tuning reports, not billed-cost accounting, provider-token telemetry, or broad context retrieval;
- stronger model-based repeated-failure diagnosis backend;
- richer semantic merge-conflict repair and broader repair strategies;
- automated pull-request creation;
- repository-instruction-driven remote execution evidence, if a target project requires it;
- stronger runtime adapters for local/open model roles.

## Documentation Ownership

- Operational instructions belong in [USER_GUIDE.md](USER_GUIDE.md).
- Contributor workflow belongs in [DEVELOPMENT.md](DEVELOPMENT.md).
- Architecture and roadmap decisions belong under [design/](design/).
- Root [README.md](../README.md) should stay short and link into this directory.
- Agent working rules belong in root [AGENTS.md](../AGENTS.md).
