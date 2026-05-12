# Known Failures

## ad-0001

- Earlier runs failed when the configured Codex model hit quota.
- Earlier verification used `.venv/bin/python` relative to the task worktree; self-test contracts now use the main worktree virtual environment by absolute path.

## Closed-loop run learning: unsupported primary worker

- The first full release run showed that `gpt-5.3-codex-spark` is not
  supported for the current Codex ChatGPT account.
- Project configs now default workers to `gpt-5.4-mini`, but doctor/preflight
  diagnostics should keep flagging unsupported or wasteful model routing before
  a release starts.

## Current Gap: budget-aware release governance

- Release metrics exist, but they are not yet converted into explicit budget
  ledgers, budget enforcement, or tuning recommendations.
- The next planned release, `release-governor-1`, addresses this Phase 2 gap
  without provider-specific pricing tables or external services.
