# rust_rockfall Known Failures and Risk Areas

- SLURM driver is new and has had quoting/heredoc/import-path regressions.
- Existing run roots may reuse chunk state; use unique roots for fresh baselines.
- Repeat JSON/provenance artifacts can drift slightly even when numerical artifacts are stable.
- Balfrin `python3` may be too old; use `uv run python` on Balfrin.
- Do not rely on ad hoc SSH one-liners for evidence.
- Docs examples have drifted from actual manifest names before; verify paths.
- Collector should remain robust when runtime manifests or logs are missing.
