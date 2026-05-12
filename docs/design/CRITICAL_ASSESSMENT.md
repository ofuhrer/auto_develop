# Critical Assessment

## Summary

The design direction is sound: a thin external orchestrator, bounded task contracts, isolated worktrees, deterministic verification, and evidence bundles are the right primitives for pragmatic agentic development in validation-heavy repositories.

The main risk is scope creep. The system can become a platform before the first autonomous task loop works. Phase 1 must stay narrow: one local CLI, one executor, one repository config, one task at a time, and autonomous finalization only when explicitly requested.

## Strengths

- Worktree isolation gives a practical recovery model for failed agent attempts.
- Task contracts make autonomy safer by replacing broad goals with bounded execution units.
- Evidence bundles reduce reliance on agent summaries.
- Deterministic verification protects against agents redefining success.
- Filesystem state is fast to implement and easy to inspect during early development.
- Explicit finalization flags preserve accountability for autonomous commit, merge, and push operations.

## Weaknesses

- The design still assumes agents will obey prompts unless the orchestrator enforces file, diff, and command boundaries.
- Domain validity cannot be proven by generic automation alone.
- Filesystem state will become awkward once many releases, attempts, and evidence bundles exist.
- Cost accounting may be approximate because provider CLIs often hide token-level usage.
- Subprocess isolation is not a real sandbox.
- A generic adapter interface may become too abstract before two real target repositories exist.

## Pragmatic Simplifications for v1

- Support one executor backend first: Codex CLI.
- Support one target repository first: `rust_rockfall`.
- Use Python dataclasses or Pydantic models, but avoid building a plugin framework until a second adapter exists.
- Store state in predictable directories and plain files.
- Implement deterministic review before model review.
- Treat model review as optional diagnosis, not a required path for every task.
- Keep project-specific remote execution out of the control plane unless the target repository's own documentation requires it.
- Do not build automatic PR creation until evidence collection and deterministic review are stable.

## Decisions to Revisit After Sprint 0

- Whether the task contract schema is too strict or too loose.
- Whether evidence bundles contain enough information to review without rerunning commands.
- Whether the executor prompt gives agents enough context without causing drift.
- Whether worktree cleanup is safe and recoverable.
- Whether verification profiles need typed command results instead of plain logs.
- Whether run indexing needs SQLite.
- Whether a second executor backend is worth adding.
- Whether a second repository adapter is needed to validate the abstraction.

## Non-Negotiables

- No task merges or pushes itself unless explicit accepted-task finalization was requested.
- No task skips verification.
- No domain fixture or tolerance changes without explicit permission.
- No release tagging without human approval.
- No secrets in logs or evidence bundles.
- No unbounded retry loops.
