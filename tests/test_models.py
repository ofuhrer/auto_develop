from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentic_devloop.models import (
    Decision,
    EvidenceBundle,
    ProjectConfig,
    ReleaseObjective,
    ReviewDecision,
    Reviewer,
    TaskContract,
    TaskRun,
    TaskState,
)
from agentic_devloop.yaml_io import load_yaml_model


ROOT = Path(__file__).resolve().parents[1]


def test_sample_yaml_files_validate() -> None:
    project = load_yaml_model(ROOT / "configs" / "rust_rockfall.yaml", ProjectConfig)
    objective = load_yaml_model(ROOT / "objectives" / "v0.8.0.yaml", ReleaseObjective)
    contract = load_yaml_model(ROOT / "contracts" / "rr-0001.yaml", TaskContract)

    assert project.project_id == "rust_rockfall"
    assert objective.release_id == "v0.8.0"
    assert contract.task_id == "rr-0001"


def test_missing_required_field_fails_clearly() -> None:
    with pytest.raises(ValidationError) as error:
        ProjectConfig.model_validate(
            {
                "project_id": "rust_rockfall",
                "repo_path": "/tmp/rust_rockfall",
                "default_base_branch": "main",
                "worktree_root": "/tmp/worktrees",
                "verification_profiles": {"default": {"commands": ["cargo test"]}},
                "budget": {
                    "max_executor_attempts_per_task": 2,
                    "max_strong_model_calls_per_release": 10,
                    "max_changed_files_per_task": 8,
                    "max_diff_lines_per_task": 600,
                },
            }
        )

    errors = error.value.errors()

    assert errors[0]["loc"] == ("executor",)
    assert errors[0]["type"] == "missing"


def test_runtime_state_models_validate() -> None:
    now = datetime.now(timezone.utc)

    run = TaskRun(
        task_id="rr-0001",
        state=TaskState.VERIFYING,
        worktree_path=Path("/tmp/worktrees/rr-0001"),
        branch="agent/v0.8.0/rr-0001",
        executor_attempts=1,
        started_at=now,
        updated_at=now,
        changed_files=[],
        diff_lines=0,
        verification_results=[],
    )
    evidence = EvidenceBundle(
        task_id="rr-0001",
        run_id="2026-05-12_v0.8.0",
        bundle_path=Path("runs/2026-05-12_v0.8.0/rr-0001"),
        contract_path=Path("runs/2026-05-12_v0.8.0/rr-0001/contract.yaml"),
        run_state_path=Path("runs/2026-05-12_v0.8.0/rr-0001/run_state.json"),
        executor_prompt_path=Path("runs/2026-05-12_v0.8.0/rr-0001/executor_prompt.md"),
        executor_stdout_path=Path("runs/2026-05-12_v0.8.0/rr-0001/executor_stdout.log"),
        executor_stderr_path=Path("runs/2026-05-12_v0.8.0/rr-0001/executor_stderr.log"),
        git_diff_path=Path("runs/2026-05-12_v0.8.0/rr-0001/git_diff.patch"),
        changed_files_path=Path("runs/2026-05-12_v0.8.0/rr-0001/changed_files.txt"),
        verification_log_path=Path("runs/2026-05-12_v0.8.0/rr-0001/verification.log"),
    )
    decision = ReviewDecision(
        task_id="rr-0001",
        decision=Decision.ACCEPTED,
        reviewer=Reviewer.DETERMINISTIC,
        rationale="Diff is within contract and verification passed.",
    )

    assert run.state == TaskState.VERIFYING
    assert evidence.task_id == "rr-0001"
    assert decision.decision == Decision.ACCEPTED
