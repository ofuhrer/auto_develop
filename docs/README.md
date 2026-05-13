# Documentation

This directory contains user, maintainer, and design documentation for `agentic-devloop`.

## Start Here

- [User Guide](USER_GUIDE.md): step-by-step instructions for using `auto_develop` on an existing development project.
- [Development Guide](DEVELOPMENT.md): setup, test, smoke-test, and documentation practices for contributors.
- [Design Documentation](design/README.md): architecture, technical specification, roadmap, and design risks.

## Project Summary

`agentic-devloop` is an autonomous-first orchestration layer around coding agents. The current governor path is split into a one-epic `GovernorLoop` service, a typed `StateStore` persistence seam, a `RepairPolicy` decision seam, typed supervisor decision records for repair/scheduling/execution-strategy/finding adjudication, a release-local feature-review/repair loop, and implemented runtime-supervisor repair/resume seams for structured release failures. Those seams support roadmap/backlog analysis for the current epic, state persistence, bounded repair decisions, contract normalization, governor-level logging, semantic review of one integrated feature branch, deterministic state-review snapshot capture (`state_review_snapshot.json`) with contract-plan plumbing through `state_review_snapshot_path`, supervisor-owned execution-strategy selection before contract generation, one-shot execution input materialization, and supervisor-owned release scheduling from overlap-risk reports. The target architecture is now explicitly a deterministic kernel plus a high-level governor/supervisor agent: deterministic code owns hard gates, Git/worktrees, verification, evidence, metrics, typed persistence, and finalization mechanics; the supervisor owns judgment-heavy choices such as one-shot versus decomposed execution, reviewer-output normalization, finding adjudication, retry/split/replan decisions, and roadmap updates. The one-epic execution-strategy seam is shipped; the broader N-epic governor loop, full pre-epic state-review decisioning from live repository evidence, and repeated-cycle state refresh remain planned.

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
- Codex CLI executor wrapper with bounded attempts and fallback models;
- `doctor` preflight diagnostics for repo cleanliness, stale worktrees, release-branch collisions, and routing warnings;
- one-epic `GovernorLoop` service boundaries for selected-epic execution and follow-up state refresh inputs;
- typed `StateStore` persistence over repo-state files and run summaries;
- `RepairPolicy` decision seams for classifying failures into retry or stop decisions;
- runtime-supervisor inputs for release events, release summaries, evidence bundles, raw logs, budget ledgers, tuning reports, and backlog-state references;
- bounded repair actions for environment repair, planner-contract normalization, task split or scope narrowing, release resume, long-running-worker inspection, model escalation, and repo-state update proposals;
- structured stop evidence when a hard gate, unavailable model, invalid release-resume request, or exhausted retry budget blocks repair;
- deterministic verification command execution;
- evidence bundle collection;
- deterministic review findings and persisted decisions;
- repeated-failure diagnosis evidence (`executor_attempts.json` and `failure_diagnosis.yaml`) with deterministic classification by default and a replaceable backend seam for stronger review;
- role-based model routing;
- repo-state context injection;
- `run-task`, `run-release`, `plan-backlog`, `plan-release`, `run-objective`, `status`, and `cleanup` commands;
- `run-backlog` for chaining backlog planning into objective and release execution;
- supervisor-owned execution-strategy selection for one-epic releases, including `one_shot`, `sequential_contracts`, `parallel_contracts`, `stacked_branches`, `patch_handoff`, `replan`, and `stop` outcomes;
- one-shot planning/input materialization: `one_shot` currently writes a bounded `one_shot_execution_input.json` and returns `release: null`; the one-shot worker execution path is still planned, so `run-backlog` keeps executable decomposition as its default until that runner exists;
- `stop` strategy recording in `execution_strategy_selection.json`; a typed `execution_strategy` supervisor decision artifact is written for executable/replan actions today, while typed blocked-decision persistence for `stop` remains planned;
- bounded planner-output normalization for repairable generated-contract drift;
- typed supervisor decision records under `runs/<run-id>/**/supervisor_decisions/` for auditable scheduling, repair, and soft-budget decisions;
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

- normalization/repair of useful planner, reviewer, and supervisor outputs before strict typed validation hard-stops execution;
- multi-epic governor looping beyond the current one-epic service boundary;
- the shipped one-epic execution-strategy seam, with the remaining gaps being one-shot worker execution and a broader N-epic governor that can consume it across repeated cycles;
- full pre-epic repository state-review decision pass over source state, branches, docs, repo-state memory, recent runs, metrics, and artifacts before selecting the next epic;
- multi-epic orchestration of the implemented release-local feature-review/repair loop;
- top-level governor log stream for monitoring an N-epic run across backlog planning, contract generation, release execution, repair, review, finalization, and state refresh;
- always-on state refresh across repeated epic cycles;
- reduction of deterministic heuristic code once supervisor-backed decisions are available; candidate areas include backlog scoring, contract-normalization heuristics, failure classification, budget-tuning prose, exact-overlap rejection, hard rejection for small budget overages, brittle verification-command assumptions, and cockpit-summary filtering;
- shared verification-runtime policy so isolated worktrees can run tests without per-worktree virtual environments;
- executor liveness classification from process, output, heartbeat, and file/diff activity rather than elapsed time alone;
- target-artifact ownership: external targets should keep durable `.auto_develop/repo_state`, objectives, contracts, and compact outcome history in the target repo or a dedicated control repo, not in the `auto_develop` source checkout;
- broader multi-epic governor automation remains planned;
- supervisor-owned scheduling decisions are implemented for normal source overlap, but stacked branch re-slicing and a broader N-epic governor loop remain planned;
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
