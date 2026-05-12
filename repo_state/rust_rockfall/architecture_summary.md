# rust_rockfall Architecture Summary

`rust_rockfall` is a Rust simulation and Python validation/orchestration project for reproducible rockfall trajectory and hazard-layer experiments.

Core pieces:

- Rust numerical kernel under `src/`.
- Python hazard/probe tooling under `scripts/`.
- Tracked probe definitions under `validation/probes/`.
- Runtime outputs under ignored roots such as `hazard/results/`, `validation/private/`, `/tmp`, or Balfrin `$SCRATCH`.
- Documentation and evidence under `docs/`.

Current focus:

- Stabilize SLURM-first Balfrin probe driver workflows.
- Make fresh vs repeat/reuse probe semantics explicit.
- Improve collector summaries so run evidence includes log warning/error information.

Autonomy constraints:

- Do not change physics, probability semantics, validation semantics, fixtures, or numerical tolerances.
- Do not submit SLURM jobs unless a task explicitly requests remote dispatch.
- Do not commit runtime outputs.
