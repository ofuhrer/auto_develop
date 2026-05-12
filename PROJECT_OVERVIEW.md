# Auto Develop Project Overview

## Purpose

Build an external, generic AI development orchestration tool for scientific software engineering.

The tool coordinates autonomous coding agents across target repositories such as `rust_rockfall`. It should move quickly, keep task scope bounded, and preserve deterministic evidence for every accepted change.

The system treats AI agents as unreliable proposal generators, not trusted developers.

## Design Priorities

1. Fast pragmatic development: ship a small local CLI first, prove the loop on one repository, and defer databases, web UI, distributed execution, and advanced model routing.
2. High autonomy inside bounded phases: agents should keep working through implementation, verification, evidence collection, and deterministic review without stopping for minor decisions.
3. Clear stopping criteria after major steps: stop for human approval only at release planning approval, risky scientific scope changes, failed bounded retries, and release tagging. Commit, merge, and push may run autonomously only when explicitly requested.
4. Deterministic verification before trust: tests, diffs, logs, changed-file lists, and budget checks are authoritative; agent summaries are supporting evidence only.
5. Filesystem-first state: use run directories, task contracts, logs, evidence bundles, and Git metadata before adding persistent services.
6. Scientific conservatism: validation fixtures, numerical tolerances, benchmarks, and scientific assumptions require explicit contract permission and review.

## Document Map

- [Architecture](docs/design/ARCHITECTURE.md): core model, state machine, autonomy policy, cost/context controls, and scientific constraints.
- [Technical Specification](docs/design/TECHNICAL_SPECIFICATION.md): data models, CLI contracts, interfaces, state layout, security, and infrastructure assumptions.
- [Roadmap and Backlog](docs/design/ROADMAP_AND_BACKLOG.md): phased implementation plan, critical path, edge cases, and Sprint 0 tasks.
- [Critical Assessment](docs/design/CRITICAL_ASSESSMENT.md): design risks, simplifications, and decisions to revisit after Sprint 0.

## Agent Operating Rules

When using this document set as implementation context:

1. Start with [Roadmap and Backlog](docs/design/ROADMAP_AND_BACKLOG.md) to identify the current task.
2. Use [Technical Specification](docs/design/TECHNICAL_SPECIFICATION.md) for schemas, CLI behavior, and file formats.
3. Use [Architecture](docs/design/ARCHITECTURE.md) for policy decisions, autonomy boundaries, and scientific safeguards.
4. Check [Critical Assessment](docs/design/CRITICAL_ASSESSMENT.md) before expanding scope or adding infrastructure.
5. Prefer the smallest working implementation that satisfies the current acceptance criteria.
6. Do not expand scope beyond the active task contract.
7. Continue autonomously until a major stop condition is reached.

## Major Stop Conditions

Agents should stop and ask for human direction only when one of these happens:

- A release objective or task contract is missing required fields and cannot be inferred safely.
- A task requires changes outside its allowed files or explicit scope.
- A scientific fixture, numerical tolerance, benchmark, or validation rule must change.
- Verification fails after the configured retry limit.
- The diff exceeds budget limits for changed files or line count.
- A release tag, deployment, or irreversible repository operation is required.
- A merge or push is required but autonomous finalization was not explicitly requested.
- Secrets, credentials, or unsafe filesystem operations are involved.

## v1 Mission

Create a pragmatic autonomous development loop that can execute release-sized objectives by decomposing them into bounded, verifiable tasks. Use cheap execution models where possible and reserve frontier models for planning, review, and failed-task diagnosis.

The v1 loop should reduce manual orchestration without allowing agent drift, task collapse, verification weakening, or unreviewed scientific changes.

## Current Implementation

The repo now contains a minimal executable loop behind `agent-loop run-task`.

Implemented:

- Project and contract schema validation.
- Project config loading.
- Git worktree creation.
- Contract-based prompt generation.
- Codex CLI executor wrapper.
- Verification command runner.
- Evidence bundle collection.
- Deterministic review with persisted `decision.yaml`.
- Optional accepted-task finalization with commit, merge, and push.
- Repo-state context loading from `repo_state/<project>/`.
- Context-aware executor prompts with character budget enforcement.
- Model-call metadata capture for prompt/output size tracking.
- Task-type verification profiles for documentation, code, benchmark, scientific validation, and release preparation.
- Scientific guardrails for fixture and tolerance changes.
- Benchmark and remote-dispatch evidence metadata.

Not yet completed:

- Automated merge-conflict repair.
- Strong-model call accounting.
- Repeated-failure diagnosis beyond deterministic escalation.
- Actual Balfrin remote execution and artifact collection.
- A real synthetic run against `rust_rockfall`.
- Sprint 0 review based on actual executor evidence.
- Pull-request automation.
