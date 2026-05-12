# Known Failures

## ad-0001

- Earlier runs failed when the configured Codex model hit quota.
- Earlier verification used `.venv/bin/python` relative to the task worktree; self-test contracts now use the main worktree virtual environment by absolute path.

## Closed-loop run learning: unsupported primary worker

- The first full release run showed that `gpt-5.3-codex-spark` is not
  supported for the current Codex ChatGPT account.
- Project configs now express the recommended model hierarchy with safe
  fallbacks and `model_catalog` availability diagnostics. `gpt-5.3-codex-spark`
  remains a repair-role candidate with fallback to `gpt-5.4-mini`.

## Current Gap: autonomous roadmap governor

- `plan-backlog --mode strong-model --execute-planner` can invoke a governor
  agent that reads docs, roadmap, repo-state memory, and goals, then emits a
  validated `BacklogPlan`.
- The next gap is chaining this into a persistent autonomous loop: backlog
  state, objective/contract generation, release execution, and repo-state
  updates after each epic.
