# Known Failures

## ad-0001

- Earlier runs failed when the configured Codex model hit quota.
- Earlier verification used `.venv/bin/python` relative to the task worktree; self-test contracts now use the main worktree virtual environment by absolute path.

## Current Gap: repeated-failure diagnosis

- Executor failures currently receive deterministic classification only.
- Verification failures are reviewed deterministically, but there is no structured repeated-failure diagnosis artifact for deciding whether to retry, narrow scope, escalate, or stop.
- The next planned release, `failure-diagnosis-1`, addresses this Phase 2 gap without adding external service dependencies.
