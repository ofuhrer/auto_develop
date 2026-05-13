from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agentic_devloop.feature_review import (
    MAX_FEATURE_REVIEW_ARTIFACT_CHARS,
    MAX_FEATURE_REVIEW_DIFF_CHARS,
    FeatureReviewContext,
    assemble_feature_review_context,
    generate_repair_contracts_for_required_findings,
    invoke_feature_reviewer,
    load_feature_review_branches,
    render_feature_review_prompt,
)
from agentic_devloop.models import (
    ExecutorConfig,
    FeatureReviewDecision,
    FeatureReviewFinding,
    FeatureReviewRecommendation,
    FeatureReviewSeverity,
    Reviewer,
    TaskContract,
)
from agentic_devloop.process import ProcessOutput


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout


def _write_project_config(tmp_path: Path, *, project_id: str, repo_path: Path) -> Path:
    config_dir = tmp_path / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / f"{project_id}.yaml"
    config_path.write_text(
        "\n".join(
            [
                f"project_id: {project_id}",
                f"repo_path: {repo_path}",
                "default_base_branch: main",
                f"worktree_root: {tmp_path / 'worktrees'}",
                "executor:",
                "  type: codex_cli",
                "  model: worker",
                "  max_walltime_minutes: 1",
                "verification_profiles:",
                "  default:",
                "    commands:",
                "      - python -m compileall src",
                "budget:",
                "  max_executor_attempts_per_task: 1",
                "  max_strong_model_calls_per_release: 10",
                "  max_changed_files_per_task: 10",
                "  max_diff_lines_per_task: 100",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_dir


def test_load_feature_review_branches_uses_config_base_and_default_feature_branch(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config_dir = _write_project_config(tmp_path, project_id="demo", repo_path=repo_path)

    branches = load_feature_review_branches(
        project_id="demo",
        release_id="rel-1",
        config_dir=config_dir,
        integration_branch=None,
    )

    assert branches.base_branch == "main"
    assert branches.integration_branch == "feature/rel-1"


def test_assemble_feature_review_context_selects_latest_release_run_and_diff(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "user.email", "test@example.com")
    _git(repo_path, "config", "user.name", "Test")

    (repo_path / "docs" / "design").mkdir(parents=True, exist_ok=True)
    (repo_path / "docs" / "design" / "architecture.md").write_text("design notes", encoding="utf-8")

    (repo_path / "hello.txt").write_text("base\n", encoding="utf-8")
    _git(repo_path, "add", "hello.txt")
    _git(repo_path, "commit", "-m", "base")

    release_id = "rel-2"
    integration_branch = f"feature/{release_id}"
    _git(repo_path, "checkout", "-b", integration_branch)
    (repo_path / "hello.txt").write_text("base\nfeature\n", encoding="utf-8")
    _git(repo_path, "add", "hello.txt")
    _git(repo_path, "commit", "-m", "feature")
    integration_commit = _git(repo_path, "rev-parse", "--verify", "HEAD").strip()

    runs_dir = repo_path / "runs"
    run_old = runs_dir / f"20260101T000000Z_{release_id}_release"
    run_new = runs_dir / f"20260201T000000Z_{release_id}_release"
    run_old.mkdir(parents=True)
    run_new.mkdir(parents=True)
    (run_old / "release_summary.json").write_text(
        json.dumps(
            {"release_id": release_id, "integration_branch": integration_branch, "tasks": []},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (run_new / "release_summary.json").write_text(
        json.dumps(
            {"release_id": release_id, "integration_branch": integration_branch, "tasks": []},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    summary_payload = json.loads((run_new / "release_summary.json").read_text(encoding="utf-8"))
    summary_payload["integration_commit"] = integration_commit
    (run_new / "release_summary.json").write_text(json.dumps(summary_payload, indent=2) + "\n", encoding="utf-8")
    (run_new / "release_review.md").write_text("# review\n", encoding="utf-8")
    (run_new / "release_metrics.json").write_text('{"ok": true}\n', encoding="utf-8")
    (run_new / "release_budget.json").write_text('{"budget": "ok"}\n', encoding="utf-8")
    (run_new / "release_tuning.md").write_text("tuning\n", encoding="utf-8")

    context = assemble_feature_review_context(
        repo_path=repo_path,
        release_id=release_id,
        base_branch="main",
        integration_branch=integration_branch,
        runs_dir=Path("runs"),
        docs_design_dir=Path("docs/design"),
    )

    assert context.latest_release_run_dir == run_new
    assert context.release_summary_path == run_new / "release_summary.json"
    assert context.release_review_path == run_new / "release_review.md"
    assert "hello.txt" in context.changed_files
    assert "+feature" in context.diff_text
    assert context.docs_design_paths

    prompt = render_feature_review_prompt(
        context=context,
        repo_path=repo_path,
        runs_dir=Path("runs"),
        docs_design_dir=Path("docs/design"),
    )
    assert release_id in prompt
    assert "Git diff" in prompt


def test_render_feature_review_prompt_truncates_large_diff_and_artifacts(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    run_dir = repo_path / "runs" / "20260101T000000Z_rel-3_release"
    run_dir.mkdir(parents=True)
    summary_path = run_dir / "release_summary.json"
    summary_path.write_text("S" * (MAX_FEATURE_REVIEW_ARTIFACT_CHARS + 100), encoding="utf-8")

    context = FeatureReviewContext(
        release_id="rel-3",
        base_branch="main",
        integration_branch="feature/rel-3",
        base_commit="a" * 40,
        integration_commit="b" * 40,
        changed_files=["src/agentic_devloop/feature_review.py"],
        diff_text="D" * (MAX_FEATURE_REVIEW_DIFF_CHARS + 100),
        docs_design_paths=[],
        latest_release_run_dir=run_dir,
        release_summary_path=summary_path,
        release_review_path=None,
        release_metrics_path=None,
        release_budget_path=None,
        release_tuning_path=None,
    )

    prompt = render_feature_review_prompt(context=context, repo_path=repo_path)

    assert "feature review context truncated: git diff exceeded" in prompt
    assert "feature review context truncated: release_summary.json exceeded" in prompt
    assert "Inspect full evidence at git diff --patch main..feature/rel-3" in prompt
    assert "Inspect full evidence at runs/20260101T000000Z_rel-3_release/release_summary.json" in prompt


def test_invoke_feature_reviewer_parses_decision_and_writes_artifacts(monkeypatch, tmp_path: Path) -> None:
    def fake_run_process(command, *, cwd, timeout_seconds, shell=False, input_text=None, **_kwargs):
        del command, cwd, timeout_seconds, shell
        payload = {
            "release_id": "rel-3",
            "reviewer": "strong_model",
            "summary": "LGTM",
            "recommendation": "approve",
            "accepted_risks": [],
            "rerun_verification_commands": [],
            "findings": [],
        }
        return ProcessOutput(
            command=["codex"],
            exit_code=0,
            stdout=json.dumps(payload) + "\n",
            stderr="",
            duration_seconds=0.1,
        )

    monkeypatch.setattr("agentic_devloop.feature_review.run_process", fake_run_process)

    output_dir = tmp_path / "feature_review"
    result = invoke_feature_reviewer(
        config=ExecutorConfig(type="codex_cli", model="reviewer", max_walltime_minutes=1),
        repo_path=tmp_path,
        prompt="review this",
        release_id="rel-3",
        output_dir=output_dir,
    )

    assert result.decision.release_id == "rel-3"
    assert result.decision.reviewer == Reviewer.STRONG_MODEL
    assert result.decision.recommendation == FeatureReviewRecommendation.APPROVE
    assert result.stdout_path.read_text(encoding="utf-8").startswith("{")
    assert result.metadata_path.exists()
    assert result.prompt_path.exists()


def test_invoke_feature_reviewer_backend_failure_returns_blocked_decision(monkeypatch, tmp_path: Path) -> None:
    def fake_run_process(command, *, cwd, timeout_seconds, shell=False, input_text=None, **_kwargs):
        del command, cwd, timeout_seconds, shell, input_text
        return ProcessOutput(
            command=["codex"],
            exit_code=2,
            stdout="",
            stderr="backend down",
            duration_seconds=0.2,
        )

    monkeypatch.setattr("agentic_devloop.feature_review.run_process", fake_run_process)

    output_dir = tmp_path / "feature_review"
    result = invoke_feature_reviewer(
        config=ExecutorConfig(type="codex_cli", model="reviewer", max_walltime_minutes=1),
        repo_path=tmp_path,
        prompt="review this",
        release_id="rel-4",
        output_dir=output_dir,
    )

    assert result.decision.recommendation == FeatureReviewRecommendation.ESCALATE
    assert result.decision.reviewer == Reviewer.DETERMINISTIC
    assert result.decision.findings
    assert "backend down" in result.decision.findings[0].summary


def test_assemble_feature_review_context_raises_when_refs_missing(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "user.email", "test@example.com")
    _git(repo_path, "config", "user.name", "Test")
    (repo_path / "hello.txt").write_text("base\n", encoding="utf-8")
    _git(repo_path, "add", "hello.txt")
    _git(repo_path, "commit", "-m", "base")

    with pytest.raises(ValueError, match="git ref not found"):
        assemble_feature_review_context(
            repo_path=repo_path,
            release_id="rel-5",
            base_branch="main",
            integration_branch="feature/rel-5",
        )


def test_generate_repair_contracts_for_required_findings_preserves_scope_and_forbidden_changes() -> None:
    source_contract = TaskContract.model_validate(
        {
            "task_id": "rel-6-0001",
            "release_id": "rel-6",
            "title": "Main task",
            "budget_class": "M",
            "objective": "Implement feature.",
            "allowed_files": ["src/foo.py", "src/bar.py"],
            "forbidden_changes": ["Do not weaken validation guards."],
            "required_evidence": ["pytest output"],
            "verification": {"commands": ["pytest tests/test_feature_review.py"]},
            "stop_conditions": ["Stop on scope drift."],
        }
    )
    decision = FeatureReviewDecision.model_validate(
        {
            "release_id": "rel-6",
            "reviewer": "strong_model",
            "summary": "Needs one required fix and one optional follow-up.",
            "recommendation": "require_repairs",
            "accepted_risks": [],
            "rerun_verification_commands": [],
            "findings": [
                {
                    "finding_id": "finding-required",
                    "severity": "high",
                    "summary": "Guard path is missing.",
                    "affected_files": ["src/foo.py"],
                    "required_repairs": ["Restore guard behavior."],
                    "optional_follow_ups": [],
                },
                {
                    "finding_id": "finding-optional",
                    "severity": "low",
                    "summary": "Optional cleanup.",
                    "affected_files": ["src/bar.py"],
                    "required_repairs": [],
                    "optional_follow_ups": ["Refactor naming."],
                },
            ],
        }
    )

    generated = generate_repair_contracts_for_required_findings(
        decision=decision,
        source_contracts=[source_contract],
    )

    assert len(generated) == 1
    repair = generated[0].suggested_contract
    assert repair.allowed_files == ["src/foo.py"]
    assert repair.forbidden_changes == ["Do not weaken validation guards."]
    assert repair.required_evidence == ["git diff", "changed-files list", "pytest output"]
    assert repair.verification.commands == ["pytest tests/test_feature_review.py"]
    assert repair.depends_on == ["rel-6-0001"]


def test_generate_repair_contracts_ignores_generated_evidence_paths_for_scope() -> None:
    source_contract = TaskContract.model_validate(
        {
            "task_id": "rel-6-0001",
            "release_id": "rel-6",
            "title": "Main task",
            "budget_class": "M",
            "objective": "Implement feature.",
            "allowed_files": ["src/foo.py"],
            "required_evidence": ["pytest output"],
            "verification": {"commands": ["pytest tests/test_feature_review.py"]},
            "stop_conditions": ["Stop on scope drift."],
        }
    )
    decision = FeatureReviewDecision.model_validate(
        {
            "release_id": "rel-6",
            "reviewer": "strong_model",
            "summary": "Needs repair with run evidence.",
            "recommendation": "require_repairs",
            "accepted_risks": [],
            "rerun_verification_commands": [],
            "findings": [
                {
                    "finding_id": "finding-required",
                    "severity": "high",
                    "summary": "Prior run evidence identifies a source issue.",
                    "affected_files": [
                        "runs/20260101T000000Z_rel-6_release/feature_review.json",
                        "src/foo.py",
                    ],
                    "evidence_paths": ["runs/20260101T000000Z_rel-6_release/feature_review.json"],
                    "required_repairs": ["Repair the source issue described by run evidence."],
                }
            ],
        }
    )

    generated = generate_repair_contracts_for_required_findings(
        decision=decision,
        source_contracts=[source_contract],
    )

    assert generated[0].suggested_contract.allowed_files == ["src/foo.py"]


def test_generate_repair_contracts_for_required_findings_stops_on_unmapped_file() -> None:
    source_contract = TaskContract.model_validate(
        {
            "task_id": "rel-7-0001",
            "release_id": "rel-7",
            "title": "Main task",
            "budget_class": "S",
            "objective": "Implement feature.",
            "allowed_files": ["src/foo.py"],
            "required_evidence": ["pytest output"],
            "verification": {"commands": ["pytest -q"]},
            "stop_conditions": ["Stop on scope drift."],
        }
    )
    decision = FeatureReviewDecision.model_validate(
        {
            "release_id": "rel-7",
            "reviewer": "deterministic",
            "summary": "Repair needed.",
            "recommendation": "require_repairs",
            "accepted_risks": [],
            "rerun_verification_commands": [],
            "findings": [
                FeatureReviewFinding(
                    finding_id="finding-required",
                    severity=FeatureReviewSeverity.HIGH,
                    summary="Touches file outside contract.",
                    affected_files=["src/other.py"],
                    required_repairs=["Fix behavior."],
                )
            ],
        }
    )

    with pytest.raises(ValueError, match="outside source contract scope"):
        generate_repair_contracts_for_required_findings(
            decision=decision,
            source_contracts=[source_contract],
        )
