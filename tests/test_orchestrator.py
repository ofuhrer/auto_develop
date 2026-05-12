from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import yaml

from agentic_devloop.failure_diagnosis import FailureDiagnosisBackendResult, FailureDiagnosisRequest
from agentic_devloop.models import (
    ExecutorConfig,
    ExecutorResult,
    FailureDiagnosis,
    FailureDiagnosisGuidance,
    FailureDiagnosisInput,
    FailureDiagnosisSourceMetadata,
    ProjectConfig,
)
from agentic_devloop import orchestrator as orchestrator_module
from agentic_devloop.orchestrator import run_task


class FakeExecutor:
    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = worktree_path / "docs" / "result.md"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("# Result\n\nImplemented by fake executor.\n", encoding="utf-8")

        stdout_path = output_dir / "executor_stdout.log"
        stderr_path = output_dir / "executor_stderr.log"
        stdout_path.write_text(f"used prompt {prompt_path}\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")

        return ExecutorResult(
            command=["fake-executor"],
            exit_code=0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            duration_seconds=0.01,
            backend="fake",
            model=None,
        )


class BenchmarkExecutor:
    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = worktree_path / "benches" / "result.txt"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("benchmark delta placeholder\n", encoding="utf-8")

        stdout_path = output_dir / "executor_stdout.log"
        stderr_path = output_dir / "executor_stderr.log"
        stdout_path.write_text("benchmark executor\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")

        return ExecutorResult(
            command=["benchmark-executor"],
            exit_code=0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            duration_seconds=0.01,
            backend="fake",
            model=None,
        )


class FailingExecutor:
    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = output_dir / "executor_stdout.log"
        stderr_path = output_dir / "executor_stderr.log"
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("usage limit\n", encoding="utf-8")

        return ExecutorResult(
            command=["fake-executor"],
            exit_code=2,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            duration_seconds=0.01,
            backend="fake",
            model=None,
        )


class TimedOutExecutor:
    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = output_dir / "executor_stdout.log"
        stderr_path = output_dir / "executor_stderr.log"
        stdout_path.write_text("still working\n", encoding="utf-8")
        stderr_path.write_text("timed out\n", encoding="utf-8")
        return ExecutorResult(
            command=["fake-executor"],
            exit_code=124,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            duration_seconds=300.0,
            timed_out=True,
            backend="fake",
            model="gpt-5.4-mini",
        )


class SizedDocsExecutor:
    def __init__(self, *, line_count: int) -> None:
        self.line_count = line_count

    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = worktree_path / "docs" / "result.md"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("\n".join(f"line-{index}" for index in range(self.line_count)) + "\n", encoding="utf-8")
        stdout_path = output_dir / "executor_stdout.log"
        stderr_path = output_dir / "executor_stderr.log"
        stdout_path.write_text(f"used prompt {prompt_path}\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return ExecutorResult(
            command=["fake-executor"],
            exit_code=0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            duration_seconds=0.01,
            backend="fake",
            model=None,
        )


class DisallowedFileExecutor:
    def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = worktree_path / "src" / "bad.py"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("print('bad')\n", encoding="utf-8")
        stdout_path = output_dir / "executor_stdout.log"
        stderr_path = output_dir / "executor_stderr.log"
        stdout_path.write_text(f"used prompt {prompt_path}\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return ExecutorResult(
            command=["fake-executor"],
            exit_code=0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            duration_seconds=0.01,
            backend="fake",
            model=None,
        )


class RecordingDiagnosisBackend:
    def __init__(self, category: str) -> None:
        self.category = category
        self.requests: list[FailureDiagnosisRequest] = []

    def diagnose(self, request: FailureDiagnosisRequest) -> FailureDiagnosisBackendResult:
        self.requests.append(request)
        return FailureDiagnosisBackendResult(
            prompt="diagnosis prompt",
            diagnosis=FailureDiagnosis(
                diagnosis_inputs=[
                    FailureDiagnosisInput(name="task_id", value=request.task.task_id),
                    FailureDiagnosisInput(
                        name="verification_exit_codes",
                        value=", ".join(str(result.exit_code) for result in request.verification_results) or "<none>",
                    ),
                ],
                category=self.category,
                confidence=0.8,
                supporting_evidence_excerpts=[],
                recommendation="Inspect the recorded failure evidence.",
                guidance=FailureDiagnosisGuidance(retryable=True, escalate=False),
                source_metadata=FailureDiagnosisSourceMetadata(
                    backend="recording-test-backend",
                    model=None,
                    command=["recording-diagnosis"],
                    exit_code=request.executor_result.exit_code,
                    timed_out=request.executor_result.timed_out,
                    stdout_path=request.executor_result.stdout_path,
                    stderr_path=request.executor_result.stderr_path,
                ),
            ),
        )


def _git(repo, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_run_task_wires_executor_verification_evidence_and_review(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {
                "type": "codex_cli",
                "model": "gpt-5.3-codex-spark",
                "max_walltime_minutes": 5,
            },
            "verification_profiles": {"default": {"commands": ["test -f docs/result.md"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )

    contract_path = tmp_path / "contract.yaml"
    _write_yaml(
        contract_path,
        {
            "task_id": "demo-0001",
            "release_id": "v0.1.0",
            "title": "Create docs result",
            "budget_class": "S",
            "objective": "Create a result document.",
            "allowed_files": ["docs/**"],
            "forbidden_changes": ["Do not edit source code."],
            "required_evidence": ["git diff", "test output"],
            "verification": {"commands": ["test -f docs/result.md"]},
            "stop_conditions": ["Verification fails twice."],
        },
    )

    result = run_task(
        project_id="demo",
        contract_path=contract_path,
        config_dir=config_dir,
        runs_dir=tmp_path / "runs",
        executor=FakeExecutor(),
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    assert result.decision.decision == "accepted"
    assert (result.bundle_path / "executor_prompt.md").exists()
    assert (result.bundle_path / "decision.yaml").exists()
    assert (result.bundle_path / "review.md").exists()
    assert (result.bundle_path / "changed_files.txt").read_text(encoding="utf-8") == "docs/result.md\n"
    assert "+Implemented by fake executor." in (result.bundle_path / "git_diff.patch").read_text(
        encoding="utf-8"
    )


def test_executor_attempts_stream_codex_output_to_progress(tmp_path, monkeypatch) -> None:
    class StreamingCodexExecutor:
        def __init__(self, config: ExecutorConfig, *, stream_callback=None, heartbeat_callback=None) -> None:
            self.config = config
            self.stream_callback = stream_callback
            self.heartbeat_callback = heartbeat_callback

        def run(self, *, prompt_path: Path, worktree_path: Path, output_dir: Path) -> ExecutorResult:
            output_dir.mkdir(parents=True, exist_ok=True)
            if self.stream_callback is not None:
                self.stream_callback("stdout", "agent says hello")
                self.stream_callback("stderr", "agent warns")
            stdout_path = output_dir / "executor_stdout.log"
            stderr_path = output_dir / "executor_stderr.log"
            stdout_path.write_text("agent says hello\n", encoding="utf-8")
            stderr_path.write_text("agent warns\n", encoding="utf-8")
            return ExecutorResult(
                command=["fake-codex"],
                exit_code=0,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                duration_seconds=0.01,
                backend="codex_cli",
                model=self.config.model,
            )

    monkeypatch.setattr(orchestrator_module, "CodexExecutor", StreamingCodexExecutor)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("prompt", encoding="utf-8")
    progress: list[str] = []

    result = orchestrator_module._run_executor_attempts(
        task_id="demo-0001",
        executor_configs=[
            ExecutorConfig(type="codex_cli", model="worker", max_walltime_minutes=1)
        ],
        executor=None,
        max_attempts=1,
        prompt_path=prompt_path,
        worktree_path=tmp_path,
        scratch_dir=tmp_path / "scratch",
        progress=progress.append,
    )

    assert result.exit_code == 0
    assert (
        "agent task=demo-0001 phase=executor attempt=1 stream=stdout | agent says hello"
        in progress
    )
    assert "agent task=demo-0001 phase=executor attempt=1 stream=stderr | agent warns" in progress


def test_conflict_repair_uses_configured_repair_role() -> None:
    config = ProjectConfig.model_validate(
        {
            "project_id": "demo",
            "repo_path": "/tmp/repo",
            "default_base_branch": "main",
            "worktree_root": "/tmp/worktrees",
            "executor": {
                "type": "codex_cli",
                "model": "gpt-5.3-codex",
                "fallback_models": ["gpt-5.4-mini"],
                "max_walltime_minutes": 5,
            },
            "model_roles": {
                "worker": {
                    "type": "codex_cli",
                    "model": "gpt-5.3-codex",
                    "fallback_models": ["gpt-5.4-mini"],
                    "max_walltime_minutes": 5,
                },
                "repair": {
                    "type": "codex_cli",
                    "model": "gpt-5.3-codex-spark",
                    "fallback_models": ["gpt-5.4-mini"],
                    "max_walltime_minutes": 5,
                }
            },
            "verification_profiles": {"default": {"commands": ["true"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        }
    )

    configs = orchestrator_module.conflict_repair_executor_configs(
        config,
        [ExecutorConfig(type="codex_cli", model="worker", max_walltime_minutes=5)],
    )

    assert [config.model for config in configs] == ["gpt-5.3-codex-spark", "gpt-5.4-mini"]


def test_run_task_uses_verification_profile_and_writes_phase3_evidence(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {
                "type": "codex_cli",
                "model": "gpt-5.3-codex-spark",
                "max_walltime_minutes": 5,
            },
            "verification_profiles": {"benchmark": {"commands": ["test -f benches/result.txt"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )
    contract_path = tmp_path / "contract.yaml"
    _write_yaml(
        contract_path,
        {
            "task_id": "bench-0001",
            "release_id": "v0.1.0",
            "title": "Record benchmark delta",
            "task_type": "benchmark",
            "budget_class": "S",
            "objective": "Record benchmark placeholder output.",
            "allowed_files": ["benches/**"],
            "required_evidence": ["git diff", "benchmark delta"],
            "verification": {"profile": "benchmark"},
            "stop_conditions": ["Verification fails twice."],
            "scientific_assumptions": ["No physical model changes."],
            "benchmark_delta_required": True,
            "remote_dispatch": {
                "target": "balfrin",
                "reason": "Heavy benchmark placeholder.",
                "required_artifacts": ["benchmark.log"],
            },
        },
    )

    result = run_task(
        project_id="demo",
        contract_path=contract_path,
        config_dir=config_dir,
        runs_dir=tmp_path / "runs",
        executor=BenchmarkExecutor(),
        now=datetime(2026, 5, 12, 12, 4, tzinfo=UTC),
    )

    assert result.decision.decision == "accepted"
    assert (result.bundle_path / "scientific_review.yaml").exists()
    assert (result.bundle_path / "benchmark_delta.json").exists()
    assert (result.bundle_path / "remote_dispatch.yaml").exists()
    assert "test -f benches/result.txt" in (result.bundle_path / "verification.log").read_text(
        encoding="utf-8"
    )


def test_run_task_can_commit_merge_and_push_accepted_changes(tmp_path) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {
                "type": "codex_cli",
                "model": "gpt-5.3-codex-spark",
                "max_walltime_minutes": 5,
            },
            "verification_profiles": {"default": {"commands": ["test -f docs/result.md"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )

    contract_path = tmp_path / "contract.yaml"
    _write_yaml(
        contract_path,
        {
            "task_id": "demo-0003",
            "release_id": "v0.1.0",
            "title": "Create docs result",
            "budget_class": "S",
            "objective": "Create a result document.",
            "allowed_files": ["docs/**"],
            "forbidden_changes": [],
            "required_evidence": ["git diff", "test output"],
            "verification": {"commands": ["test -f docs/result.md"]},
            "stop_conditions": ["Verification fails twice."],
        },
    )

    result = run_task(
        project_id="demo",
        contract_path=contract_path,
        config_dir=config_dir,
        runs_dir=tmp_path / "runs",
        executor=FakeExecutor(),
        now=datetime(2026, 5, 12, 12, 2, tzinfo=UTC),
        push_on_accept=True,
        commit_message="Add generated docs result",
    )

    assert result.decision.decision == "accepted"
    assert result.finalize is not None
    assert result.finalize.commit_hash is not None
    assert result.finalize.merged is True
    assert result.finalize.pushed is True
    assert result.finalize.lock_path is not None
    assert result.finalize.rebased_onto is not None
    assert (repo / "docs" / "result.md").read_text(encoding="utf-8").startswith("# Result")
    assert (result.bundle_path / "finalization.yaml").exists()
    assert "prompt_chars: 0" not in (result.bundle_path / "model_call_metadata.json").read_text(
        encoding="utf-8"
    )

    remote_main = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert remote_main == result.finalize.commit_hash


def test_run_task_switches_to_base_branch_before_merge(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "switch", "-c", "side")

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {
                "type": "codex_cli",
                "model": "gpt-5.3-codex-spark",
                "max_walltime_minutes": 5,
            },
            "verification_profiles": {"default": {"commands": ["test -f docs/result.md"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )
    contract_path = tmp_path / "contract.yaml"
    _write_yaml(
        contract_path,
        {
            "task_id": "demo-0004",
            "release_id": "v0.1.0",
            "title": "Create docs result",
            "budget_class": "S",
            "objective": "Create a result document.",
            "allowed_files": ["docs/**"],
            "forbidden_changes": [],
            "required_evidence": ["git diff", "test output"],
            "verification": {"commands": ["test -f docs/result.md"]},
            "stop_conditions": ["Verification fails twice."],
        },
    )

    result = run_task(
        project_id="demo",
        contract_path=contract_path,
        config_dir=config_dir,
        runs_dir=tmp_path / "runs",
        executor=FakeExecutor(),
        now=datetime(2026, 5, 12, 12, 3, tzinfo=UTC),
        merge_on_accept=True,
        commit_message="Add generated docs result",
    )

    current_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert result.finalize is not None
    assert result.finalize.merged is True
    assert current_branch == "main"
    assert (repo / "docs" / "result.md").exists()


def test_run_task_merges_into_local_feature_branch_with_remote_present(tmp_path) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "branch", "feature/demo")

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {
                "type": "codex_cli",
                "model": "gpt-5.3-codex-spark",
                "max_walltime_minutes": 5,
            },
            "verification_profiles": {"default": {"commands": ["test -f docs/result.md"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )
    contract_path = tmp_path / "contract.yaml"
    _write_yaml(
        contract_path,
        {
            "task_id": "demo-0005",
            "release_id": "v0.1.0",
            "title": "Create docs result",
            "budget_class": "S",
            "objective": "Create a result document.",
            "allowed_files": ["docs/**"],
            "forbidden_changes": [],
            "required_evidence": ["git diff", "test output"],
            "verification": {"commands": ["test -f docs/result.md"]},
            "stop_conditions": ["Verification fails twice."],
        },
    )

    result = run_task(
        project_id="demo",
        contract_path=contract_path,
        config_dir=config_dir,
        runs_dir=tmp_path / "runs",
        executor=FakeExecutor(),
        now=datetime(2026, 5, 12, 12, 5, tzinfo=UTC),
        base_branch="feature/demo",
        merge_on_accept=True,
        commit_message="Add generated docs result",
    )

    assert result.decision.decision == "accepted"
    assert result.finalize is not None
    assert result.finalize.merged is True
    feature_file = subprocess.run(
        ["git", "show", "feature/demo:docs/result.md"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert feature_file.startswith("# Result")


def test_run_task_escalates_executor_failure_without_verification(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {
                "type": "codex_cli",
                "model": "gpt-5.3-codex-spark",
                "max_walltime_minutes": 5,
            },
            "verification_profiles": {"default": {"commands": ["false"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )
    contract_path = tmp_path / "contract.yaml"
    _write_yaml(
        contract_path,
        {
            "task_id": "demo-0002",
            "release_id": "v0.1.0",
            "title": "Fail before verification",
            "budget_class": "S",
            "objective": "Do not reach verification.",
            "allowed_files": ["README.md"],
            "forbidden_changes": [],
            "required_evidence": ["git diff", "test output"],
            "verification": {"commands": ["false"]},
            "stop_conditions": ["Verification fails twice."],
        },
    )

    result = run_task(
        project_id="demo",
        contract_path=contract_path,
        config_dir=config_dir,
        runs_dir=tmp_path / "runs",
        executor=FailingExecutor(),
        now=datetime(2026, 5, 12, 12, 1, tzinfo=UTC),
    )

    assert result.decision.decision == "escalated"
    assert result.decision.rationale == "Executor failed with exit code 2."
    assert (
        result.bundle_path / "verification.log"
    ).read_text(encoding="utf-8") == "Verification skipped because executor failed.\n"
    failure_diagnosis = yaml.safe_load(
        (result.bundle_path / "failure_diagnosis.yaml").read_text(encoding="utf-8")
    )
    assert failure_diagnosis["category"] == "model_quota"
    assert failure_diagnosis["recommendation"] == "Retry with a fallback model or after the quota resets."
    assert failure_diagnosis["source_metadata"]["backend"] == "deterministic_failure_diagnosis"
    assert len(failure_diagnosis["source_metadata"]["attempts"]) == 2
    assert failure_diagnosis["final_exit_code"] == 2
    assert len(failure_diagnosis["attempts"]) == 2


def test_run_task_writes_injected_diagnosis_for_verification_failure(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {
                "type": "codex_cli",
                "model": "gpt-5.3-codex-spark",
                "max_walltime_minutes": 5,
            },
            "verification_profiles": {"default": {"commands": ["false"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )
    contract_path = tmp_path / "contract.yaml"
    _write_yaml(
        contract_path,
        {
            "task_id": "demo-0006",
            "release_id": "v0.1.0",
            "title": "Fail verification",
            "budget_class": "S",
            "objective": "Create a result document but fail verification.",
            "allowed_files": ["docs/**"],
            "forbidden_changes": [],
            "required_evidence": ["git diff", "test output", "verification failure diagnosis test"],
            "verification": {"commands": ["false"]},
            "stop_conditions": ["Verification fails twice."],
        },
    )
    backend = RecordingDiagnosisBackend(category="injected_verification_failure")

    result = run_task(
        project_id="demo",
        contract_path=contract_path,
        config_dir=config_dir,
        runs_dir=tmp_path / "runs",
        executor=FakeExecutor(),
        now=datetime(2026, 5, 12, 12, 6, tzinfo=UTC),
        failure_diagnosis_backend=backend,
    )

    assert result.decision.decision == "failed"
    assert result.decision.rationale == "Verification failed."
    assert len(backend.requests) == 1
    assert [command.exit_code for command in backend.requests[0].verification_results] == [1]
    assert backend.requests[0].changed_files == ["docs/result.md"]
    failure_diagnosis = yaml.safe_load(
        (result.bundle_path / "failure_diagnosis.yaml").read_text(encoding="utf-8")
    )
    assert failure_diagnosis["category"] == "injected_verification_failure"
    assert failure_diagnosis["source_metadata"]["backend"] == "recording-test-backend"


def test_run_task_keeps_failure_diagnosis_yaml_artifact_compatible(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {
                "type": "codex_cli",
                "model": "gpt-5.3-codex-spark",
                "max_walltime_minutes": 5,
            },
            "verification_profiles": {"default": {"commands": ["false"]}},
            "budget": {
                "max_executor_attempts_per_task": 1,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )
    contract_path = tmp_path / "contract.yaml"
    _write_yaml(
        contract_path,
        {
            "task_id": "demo-0007",
            "release_id": "v0.1.0",
            "title": "Fail before verification",
            "budget_class": "S",
            "objective": "Do not reach verification.",
            "allowed_files": ["README.md"],
            "forbidden_changes": [],
            "required_evidence": ["git diff", "test output", "backward compatibility test"],
            "verification": {"commands": ["false"]},
            "stop_conditions": ["Executor fails."],
        },
    )

    result = run_task(
        project_id="demo",
        contract_path=contract_path,
        config_dir=config_dir,
        runs_dir=tmp_path / "runs",
        executor=FailingExecutor(),
        now=datetime(2026, 5, 12, 12, 7, tzinfo=UTC),
    )

    failure_diagnosis_path = result.bundle_path / "failure_diagnosis.yaml"
    failure_diagnosis = yaml.safe_load(failure_diagnosis_path.read_text(encoding="utf-8"))
    assert failure_diagnosis_path.name == "failure_diagnosis.yaml"
    assert set(["category", "recommendation", "final_exit_code", "attempts"]).issubset(
        failure_diagnosis
    )
    assert failure_diagnosis["category"] == "model_quota"
    assert failure_diagnosis["final_exit_code"] == 2
    assert failure_diagnosis["attempts"] == [
        {"attempt": 1, "model": None, "exit_code": 2, "timed_out": False}
    ]


def test_run_task_records_runtime_supervisor_long_running_worker_inspection(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {
                "type": "codex_cli",
                "model": "gpt-5.4-mini",
                "max_walltime_minutes": 5,
            },
            "verification_profiles": {"default": {"commands": ["false"]}},
            "budget": {
                "max_executor_attempts_per_task": 1,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )
    contract_path = tmp_path / "contract.yaml"
    _write_yaml(
        contract_path,
        {
            "task_id": "demo-0008",
            "release_id": "v0.1.0",
            "title": "Timeout task",
            "budget_class": "S",
            "objective": "Timeout before verification.",
            "allowed_files": ["README.md"],
            "forbidden_changes": [],
            "required_evidence": ["git diff", "test output"],
            "verification": {"commands": ["false"]},
            "stop_conditions": ["Executor fails."],
        },
    )

    result = run_task(
        project_id="demo",
        contract_path=contract_path,
        config_dir=config_dir,
        runs_dir=tmp_path / "runs",
        executor=TimedOutExecutor(),
        now=datetime(2026, 5, 12, 12, 8, tzinfo=UTC),
    )

    failure_diagnosis = yaml.safe_load(
        (result.bundle_path / "failure_diagnosis.yaml").read_text(encoding="utf-8")
    )
    runtime_supervisor = failure_diagnosis["runtime_supervisor"]
    assert runtime_supervisor["classification"] == "long_running_worker_active"
    assert runtime_supervisor["inspection"]["applied"] is True
    assert runtime_supervisor["inspection"]["action_kind"] == "long_running_worker_inspection"
    assert runtime_supervisor["inspection"]["active"] is True


def test_run_task_records_runtime_supervisor_model_escalation_stop_when_budget_exhausted(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {
                "type": "codex_cli",
                "model": "gpt-5.4-mini",
                "max_walltime_minutes": 5,
            },
            "model_roles": {
                "worker": {
                    "type": "codex_cli",
                    "model": "gpt-5.4-mini",
                    "max_walltime_minutes": 5,
                },
                "escalation": {
                    "type": "codex_cli",
                    "model": "gpt-5.5",
                    "max_walltime_minutes": 5,
                },
            },
            "model_routing": {
                "default_role": "worker",
                "escalation_role": "escalation",
            },
            "verification_profiles": {"default": {"commands": ["false"]}},
            "budget": {
                "max_executor_attempts_per_task": 1,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 600,
            },
        },
    )
    contract_path = tmp_path / "contract.yaml"
    _write_yaml(
        contract_path,
        {
            "task_id": "demo-0009",
            "release_id": "v0.1.0",
            "title": "Exhaust attempts",
            "budget_class": "S",
            "objective": "Exhaust attempts before verification.",
            "allowed_files": ["README.md"],
            "forbidden_changes": [],
            "required_evidence": ["git diff", "test output"],
            "verification": {"commands": ["false"]},
            "stop_conditions": ["Executor fails."],
        },
    )

    result = run_task(
        project_id="demo",
        contract_path=contract_path,
        config_dir=config_dir,
        runs_dir=tmp_path / "runs",
        executor=FailingExecutor(),
        now=datetime(2026, 5, 12, 12, 9, tzinfo=UTC),
    )

    failure_diagnosis = yaml.safe_load(
        (result.bundle_path / "failure_diagnosis.yaml").read_text(encoding="utf-8")
    )
    runtime_supervisor = failure_diagnosis["runtime_supervisor"]
    assert runtime_supervisor["classification"] == "model_capability_mismatch"
    assert runtime_supervisor["model_escalation"]["applied"] is False
    assert runtime_supervisor["model_escalation"]["available_models"] == ["gpt-5.5"]
    assert runtime_supervisor["model_escalation"]["stop_kind"] == "exceeds_retry_budget"


def test_run_task_accepts_minor_budget_overage_with_soft_gate_artifact(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {"type": "codex_cli", "model": "gpt-5.3-codex-spark", "max_walltime_minutes": 5},
            "verification_profiles": {"default": {"commands": ["test -f docs/result.md"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 20,
            },
        },
    )
    contract_path = tmp_path / "contract.yaml"
    _write_yaml(
        contract_path,
        {
            "task_id": "demo-0010",
            "release_id": "v0.1.0",
            "title": "Minor overage",
            "budget_class": "S",
            "objective": "Create docs result with slight diff overage.",
            "allowed_files": ["docs/**"],
            "forbidden_changes": [],
            "required_evidence": ["git diff", "changed-files list"],
            "verification": {"commands": ["test -f docs/result.md"]},
            "stop_conditions": ["Verification fails twice."],
        },
    )
    result = run_task(
        project_id="demo",
        contract_path=contract_path,
        config_dir=config_dir,
        runs_dir=tmp_path / "runs",
        executor=SizedDocsExecutor(line_count=20),
        now=datetime(2026, 5, 12, 12, 10, tzinfo=UTC),
    )

    assert result.decision.decision == "accepted"
    assert result.decision.reviewer == "hybrid"
    payload = json.loads((result.bundle_path / "soft_gate_decision.json").read_text(encoding="utf-8"))
    assert payload["finding"]["severity"] == "low"
    assert payload["decision"]["decision"] == "accept_with_mitigation"
    assert payload["decision"]["validators_rerun"] == ["verification", "allowed_files", "scientific_review"]


def test_run_task_rejects_severe_budget_overage_with_soft_gate_artifact(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {"type": "codex_cli", "model": "gpt-5.3-codex-spark", "max_walltime_minutes": 5},
            "verification_profiles": {"default": {"commands": ["test -f docs/result.md"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 20,
            },
        },
    )
    contract_path = tmp_path / "contract.yaml"
    _write_yaml(
        contract_path,
        {
            "task_id": "demo-0011",
            "release_id": "v0.1.0",
            "title": "Severe overage",
            "budget_class": "S",
            "objective": "Create docs result with severe diff overage.",
            "allowed_files": ["docs/**"],
            "forbidden_changes": [],
            "required_evidence": ["git diff", "changed-files list"],
            "verification": {"commands": ["test -f docs/result.md"]},
            "stop_conditions": ["Verification fails twice."],
        },
    )
    result = run_task(
        project_id="demo",
        contract_path=contract_path,
        config_dir=config_dir,
        runs_dir=tmp_path / "runs",
        executor=SizedDocsExecutor(line_count=30),
        now=datetime(2026, 5, 12, 12, 11, tzinfo=UTC),
    )

    assert result.decision.decision == "needs_revision"
    payload = json.loads((result.bundle_path / "soft_gate_decision.json").read_text(encoding="utf-8"))
    assert payload["finding"]["severity"] == "high"
    assert payload["decision"]["decision"] == "reject"


def test_run_task_keeps_verification_failure_hard_failed_without_soft_bypass(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {"type": "codex_cli", "model": "gpt-5.3-codex-spark", "max_walltime_minutes": 5},
            "verification_profiles": {"default": {"commands": ["false"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 20,
            },
        },
    )
    contract_path = tmp_path / "contract.yaml"
    _write_yaml(
        contract_path,
        {
            "task_id": "demo-0012",
            "release_id": "v0.1.0",
            "title": "Verification failure",
            "budget_class": "S",
            "objective": "Fail verification even if over budget.",
            "allowed_files": ["docs/**"],
            "forbidden_changes": [],
            "required_evidence": ["git diff", "changed-files list"],
            "verification": {"commands": ["false"]},
            "stop_conditions": ["Verification fails twice."],
        },
    )
    result = run_task(
        project_id="demo",
        contract_path=contract_path,
        config_dir=config_dir,
        runs_dir=tmp_path / "runs",
        executor=SizedDocsExecutor(line_count=30),
        now=datetime(2026, 5, 12, 12, 12, tzinfo=UTC),
    )

    assert result.decision.decision == "failed"
    assert not (result.bundle_path / "soft_gate_decision.json").exists()


def test_run_task_keeps_disallowed_files_hard_needs_revision_without_soft_acceptance(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "demo.yaml",
        {
            "project_id": "demo",
            "repo_path": str(repo),
            "default_base_branch": "main",
            "worktree_root": str(tmp_path / "worktrees"),
            "executor": {"type": "codex_cli", "model": "gpt-5.3-codex-spark", "max_walltime_minutes": 5},
            "verification_profiles": {"default": {"commands": ["test -f src/bad.py"]}},
            "budget": {
                "max_executor_attempts_per_task": 2,
                "max_strong_model_calls_per_release": 10,
                "max_changed_files_per_task": 8,
                "max_diff_lines_per_task": 1,
            },
        },
    )
    contract_path = tmp_path / "contract.yaml"
    _write_yaml(
        contract_path,
        {
            "task_id": "demo-0013",
            "release_id": "v0.1.0",
            "title": "Disallowed file",
            "budget_class": "S",
            "objective": "Write outside allowed files.",
            "allowed_files": ["docs/**"],
            "forbidden_changes": [],
            "required_evidence": ["git diff", "changed-files list"],
            "verification": {"commands": ["test -f src/bad.py"]},
            "stop_conditions": ["Verification fails twice."],
        },
    )
    result = run_task(
        project_id="demo",
        contract_path=contract_path,
        config_dir=config_dir,
        runs_dir=tmp_path / "runs",
        executor=DisallowedFileExecutor(),
        now=datetime(2026, 5, 12, 12, 13, tzinfo=UTC),
    )

    assert result.decision.decision == "needs_revision"
    assert "outside allowed paths" in result.decision.rationale
    assert not (result.bundle_path / "soft_gate_decision.json").exists()


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
