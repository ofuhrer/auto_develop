# Five-Cycle Dogfood Review

Date: 2026-05-14

This note summarizes the manual five-epic dogfood effort that used `auto_develop`
to develop `auto_develop` one epic at a time:

1. `governor-state-refresh`
2. `planner-admission-repair`
3. `governor-cockpit-v2`
4. `soft-scope-budget-policy`
5. `review-convergence-adjudicator`

The goal of the review is not to relitigate each patch. The purpose is to extract
operational lessons about cost, runtime, autonomy gaps, and the next engineering
tasks required for a robust multi-epic autonomous loop.

## Aggregate Metrics

Measured from the retained release metrics for the five-cycle effort, including
the three `review-convergence-adjudicator` release attempts required to finish the
last epic:

| Metric | Value |
| --- | ---: |
| Release attempts analyzed | 7 |
| Worker/reviewer task runs | 51 |
| Accepted task runs | 50 |
| Executor attempts | 51 |
| Executor wall time | 183.5 minutes |
| Verification wall time | 28.0 minutes |
| Prompt volume | 1.84M characters |
| Injected context volume | 1.57M characters |
| Worker stdout volume | 95.9k characters |
| Raw stderr/audit chatter | 25.3M characters |
| Diff lines produced | 5,871 |
| Changed-file count across task bundles | 147 |

Model-attempt distribution:

| Model | Attempts | Executor Time | Prompt Chars | Raw Stderr |
| --- | ---: | ---: | ---: | ---: |
| `gpt-5.2` | 15 | 86.1 min | 558k | 9.73 MB |
| `gpt-5.3-codex` | 31 | 84.4 min | 1.11M | 11.65 MB |
| `gpt-5.4-mini` | 5 | 13.0 min | 169k | 3.95 MB |

Notes:

- Dollar cost is not available from local artifacts; the system records character
  counts and model attempts, not provider token billing.
- Raw stderr is dominated by executor/plugin warnings and streamed reasoning/patch
  chatter. It is useful for audit, but it is a poor cost/debug signal unless
  summarized.
- Context size repeatedly sat near the configured ceiling: 27k-33k chars per task.
  This is too high for routine repair tasks and directly increases model latency
  and cost.

## Per-Epic Outcome

| Epic | Outcome | Task Runs | Main Intervention |
| --- | --- | ---: | --- |
| `governor-state-refresh` | Accepted manually after failed release | 5 | Recovered documentation task from evidence after worktree-local `.venv` verification failed. |
| `planner-admission-repair` | Accepted | 10 | Committed generated package, patched worktree-safe verification commands, cleaned stale branch/worktree. |
| `governor-cockpit-v2` | Accepted manually after review limit | 9 | Fixed final reviewer findings after convergence limit despite green deterministic verification. |
| `soft-scope-budget-policy` | Accepted manually after review limit | 9 | Accepted/fixed scope-risk review findings; raised context budget after generated context exceeded 30k. |
| `review-convergence-adjudicator` | Accepted manually after repeated review churn | 18 across three attempts | Added manual scope-risk acceptance, multiple repair waves, final direct fixes, full-suite verification, and state update. |

## Cost Findings

1. Context is the largest avoidable cost driver.
   Most tasks received roughly the same 27k-33k character context bundle,
   including repair tasks that needed only a narrow slice. This inflated every
   worker attempt and made reviewer/repair loops expensive.

2. Review churn is more expensive than initial implementation.
   The last epic needed the original four tasks plus repeated review reruns and
   repair waves. The implemented code improved, but the marginal value of late
   review waves dropped sharply after final integration verification passed.

3. Current cost accounting is insufficient for model tuning.
   `release_metrics.json` captures prompt/output character counts and executor
   durations, but not actual token counts, billed cost, cache hits, model request
   IDs, or cost by phase. This prevents reliable comparison between one-shot,
   decomposed, reviewer, and repair strategies.

4. Raw audit streams are too noisy for cost analysis.
   The five-cycle effort generated about 25MB of raw stderr. That is useful for
   forensic audit but not for decision-making. Human-facing logs need structured
   summaries and cost counters, while raw logs should remain archival.

5. `gpt-5.2` should be treated as an expensive supervisor/reviewer model.
   It accounted for fewer attempts than `gpt-5.3-codex` but similar total executor
   time. That is appropriate for review/supervision, but wasteful when used for
   narrow mechanical repair.

## Runtime and Efficiency Findings

1. The five-cycle effort did not behave like unattended multi-epic execution.
   It required repeated manual commits of planning artifacts, reruns, final
   adjudication, scope-risk acceptance, branch merges, and repo-state updates.

2. Long-running workers were usually active, not stuck.
   Heartbeats were useful. Raw audit showed workers patching and testing. The
   missing layer is automatic classification of active, quiet-alive, stalled,
   hung, and environment-blocked states.

3. Verification time became material in later cycles.
   Verification took 28 minutes total, with several reruns driven by review repair
   waves. The system needs verification result reuse and evidence-aware reviewer
   prompts so reviewers do not demand proof that already exists.

4. Sequential execution was safe but slow.
   The scheduler serialized overlap-heavy tasks correctly, but the governor still
   lacks the authority to choose one-shot execution or a more efficient DAG when
   overlap is intentional and manageable.

5. Accepted work was sometimes stranded behind finalization gates.
   `review-convergence-adjudicator` had accepted implementation tasks but stopped
   behind scope-risk and review gates. The continuation path worked, but human
   judgment still decided when to stop rerunning.

## Manual Intervention Findings

Manual intervention occurred in five recurring categories:

1. Planning package ownership.
   Generated objectives/contracts/repo-state changes had to be committed before
   release execution because the controller repo is also the target repo.

2. Verification runtime drift.
   Isolated worktrees do not contain `.venv`. Several contracts or workers assumed
   worktree-local `.venv/bin/python`; successful runs used the shared main-runtime
   interpreter plus `PYTHONPATH=src`.

3. Scope-budget adjudication.
   A 699-line cohesive diff exceeded the 600-line soft limit. The deterministic
   placeholder correctly blocked finalization, but a supervisor/human had to
   accept it with guards.

4. Review convergence.
   Review agents repeatedly found new adjacent issues after prior repairs and
   passing verification. Without a stronger stop/adjudication policy, the system
   can spend substantial time and money chasing review asymptotes.

5. State and branch lifecycle.
   Humans still finalized merges, deleted branches, recorded completion, and
   translated run learnings into roadmap/backlog memory.

## Problems and Issues

1. Worktree runtime assumptions are still the most concrete blocker.
   A fully autonomous system cannot rely on each task worktree containing a venv.
   Verification needs an explicit project runtime policy.

2. Review agents lack bounded authority semantics.
   The reviewer can keep discovering valid adjacent improvements. That is useful
   during review, but the governor needs policy to decide when enough verified
   quality has been reached.

3. Current context slicing is too coarse.
   Repair tasks received the same large context as implementation tasks. Context
   should be phase-specific and evidence-driven.

4. Metrics are present but not yet operational.
   The data exists in `release_metrics.json`, `release_budget.json`, and tuning
   reports, but the governor does not yet use it to choose one-shot vs decomposed
   execution, adjust task size, cap review waves, or route models.

5. Logs are split by run/release.
   `release.log` is useful for one release, but the desired cockpit is a single
   top-level stream for a multi-epic governor run with child release summaries,
   repair decisions, cost counters, and stop/intervention cues.

6. Self-development hides controller/target boundary problems.
   Because `auto_develop` is both controller and target, generated artifacts and
   repo-state changes dirty the same checkout. External target repos need durable
   target-owned or control-repo-owned state.

## Architectural Consequences

The five-cycle effort supports the current architectural direction, but with a
clear rebalancing:

- Deterministic code should keep owning Git safety, worktrees, evidence capture,
  verification execution, schema validation, and finalization mechanics.
- High-level supervisor/governor agents should own judgment-heavy decisions:
  execution strategy, task granularity, scope-risk acceptance, review-wave
  continuation, reviewer-finding deferral, and when to stop chasing marginal
  review findings.
- Hard gates should remain hard only for safety and reproducibility invariants:
  forbidden paths, destructive operations, missing required evidence, failed
  verification, unsafe credentials/network policy, and invalid persisted state.
- Soft gates should become typed supervisor decisions with bounded validation:
  file/diff budget overages, normal source overlap, reviewer ambiguity, context
  pressure, and repair strategy selection.

## Distilled Development Tasks

Priority 1: Shared Verification Runtime

- Add explicit `verification_runtime` config for local interpreter, env vars,
  `PYTHONPATH`, dependency bootstrap, and command wrappers.
- Rewrite generated verification commands through that runtime.
- Ensure workers receive the same runtime policy in prompts.
- Add smoke tests proving isolated worktrees can run project tests without local
  `.venv`.

Priority 2: Review-Stop and Adjudication Policy

- Add governor policy for maximum review waves, maximum marginal findings, and
  "verified enough" acceptance after final integration verification.
- Persist a typed review-stop decision that records accepted risks, deferred
  findings, reviewer limitations, verification evidence, and why no further repair
  wave is launched.
- Teach the governor to prefer backlog deferral over repair when findings are
  policy/observability refinements rather than correctness blockers.

Priority 3: Cost and Runtime Governor

- Add phase-level cost records: planner, worker, reviewer, repair, verification,
  normalization, and finalization.
- Track real token counts and billed cost when backend metadata exposes them.
- Record model request IDs, cache status, and retry/fallback reasons.
- Use metrics to select one-shot vs decomposed strategy and to cap review/repair
  loops.

Priority 4: Context Slimming and Retrieval

- Build smaller phase-specific context bundles.
- Give repair tasks only the finding, touched files, relevant tests, and prior
  evidence rather than the full roadmap/design bundle.
- Add context budget warnings as governor inputs, not just tuning-report prose.

Priority 5: Multi-Epic Cockpit Log

- Create one parent `governor.log` for the whole N-epic run.
- Include child release starts/stops, accepted tasks, repair waves, review-stop
  decisions, cost counters, current model, elapsed time, and explicit intervention
  cues.
- Keep raw logs per child run for audit only.

Priority 6: Target Artifact Ownership

- Move durable target memory into the target repo or a configured control repo.
- Keep raw `runs/` local/archival.
- Ensure deleting and recloning `auto_develop` does not lose target backlog,
  completed-epic history, objectives, or current operating state.

Priority 7: Autonomous Finalization Loop

- Let the governor commit planning packages, execute the release, adjudicate soft
  gates, run final review, merge/push/delete branches by policy, update repo-state,
  and continue to the next epic without human intervention.
- Stop only on exhausted repair budget, hard safety policy, missing credentials,
  failed verification that cannot be repaired, or no actionable work.

## Practical Next Step

The next implementation epic should remain `shared-verification-runtime`.
It directly removes the most common manual intervention and is a prerequisite for
reliable external-target dogfooding. The second epic should be a narrower
`review-stop-policy-hardening` follow-up, because the latest cycle proved that
the first adjudication implementation still allows expensive reviewer churn.
