from __future__ import annotations

from agentic_devloop.failure_diagnosis import (
    DEFAULT_BACKEND_NAME,
    DeterministicFailureDiagnosisBackend,
    FailureDiagnosisRequest,
    build_failure_diagnosis_prompt,
)
from agentic_devloop.models import CommandResult, ExecutorAttempt, ExecutorResult, TaskContract


def test_build_failure_diagnosis_prompt_is_bounded_and_includes_evidence(tmp_path) -> None:
    task = _task_contract()
    stdout_path, stderr_path, verification_log_path = _write_logs(tmp_path)
    request = FailureDiagnosisRequest(
        task=task,
        executor_result=_executor_result(stdout_path, stderr_path, exit_code=1),
        verification_results=[
            CommandResult(command=f"pytest test_{index}.py", exit_code=1 if index < 2 else 0, stdout_path=None, stderr_path=None, duration_seconds=1.0, timed_out=False)
            for index in range(3)
        ],
        changed_files=[f"src/file_{index}.py" for index in range(4)],
        verification_log_path=verification_log_path,
        max_prompt_chars=2500,
        log_excerpt_chars=160,
    )

    prompt = build_failure_diagnosis_prompt(request)

    assert len(prompt) <= 2500
    for text in ("# Failure Diagnosis Prompt", "task_id: fd-0002", "allowed_files: src/**, tests/**", "verification_profile: default", "- attempt 1: backend=codex_cli", "command=pytest test_0.py exit_code=1", "src/file_0.py", "verification.log"):
        assert text in prompt


def test_deterministic_backend_reports_contract_mismatch(tmp_path) -> None:
    task = _task_contract()
    stdout_path, stderr_path, _ = _write_logs(tmp_path, stdout_text="stdout\n", stderr_text="stderr\n")
    result = DeterministicFailureDiagnosisBackend().diagnose(
        FailureDiagnosisRequest(
            task=task,
            executor_result=_executor_result(stdout_path, stderr_path, exit_code=1, attempts=()),
            verification_results=[],
            changed_files=["src/allowed.py", "docs/outside-contract.md"],
        )
    )

    assert result.diagnosis.category == "contract_mismatch"
    assert result.diagnosis.guidance.retryable is False
    assert result.diagnosis.guidance.escalate is True
    assert result.diagnosis.source_metadata.backend == DEFAULT_BACKEND_NAME
    assert result.diagnosis.source_metadata.command == ["deterministic-failure-diagnosis"]
    assert result.diagnosis.source_metadata.attempts[0].attempt == 1
    assert result.diagnosis.diagnosis_inputs[-1].name == "executor_attempt_count"
    assert result.diagnosis.diagnosis_inputs[-1].value == "1"
    assert result.diagnosis.recommendation.startswith("Narrow the task contract")


def test_deterministic_backend_is_stable_for_repeated_timeout_failures(tmp_path) -> None:
    task = _task_contract()
    stdout_path, stderr_path, _ = _write_logs(
        tmp_path,
        stdout_text="stdout timeout\n",
        stderr_text="executor timed out after 600 seconds\n",
    )
    request = FailureDiagnosisRequest(
        task=task,
        executor_result=_executor_result(stdout_path, stderr_path, exit_code=124, timed_out=True, attempts=_timeout_attempts(stdout_path, stderr_path)),
        verification_results=[],
        changed_files=[],
    )
    backend = DeterministicFailureDiagnosisBackend()
    first = backend.diagnose(request)
    second = backend.diagnose(request)

    assert first.prompt == second.prompt
    assert first.diagnosis.model_dump(mode="json") == second.diagnosis.model_dump(mode="json")
    assert first.diagnosis.category == "timeout"
    assert first.diagnosis.guidance.retryable is True
    assert first.diagnosis.guidance.escalate is True


def _executor_result(stdout_path, stderr_path, *, exit_code: int, timed_out: bool = False, attempts=()):
    return ExecutorResult(
        command=["codex", "exec"],
        exit_code=exit_code,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        duration_seconds=12.5 if not timed_out else 600.0,
        timed_out=timed_out,
        backend="codex_cli",
        model="gpt-5.3-codex-spark",
        prompt_chars=900,
        stdout_chars=400,
        stderr_chars=900,
        attempts=list(attempts),
    )


def _timeout_attempts(stdout_path, stderr_path):
    return [
        ExecutorAttempt(attempt=index, backend="codex_cli", model="gpt-5.3-codex-spark", command=["codex", "exec"], exit_code=124, stdout_path=stdout_path, stderr_path=stderr_path, duration_seconds=600.0, timed_out=True)
        for index in (1, 2)
    ]


def _write_logs(tmp_path, *, stdout_text="stdout line\n" + "x" * 400, stderr_text="stderr line\n" + "y" * 900, verification_text="verification line\n" + "z" * 500):
    stdout_path = tmp_path / "executor_stdout.log"
    stderr_path = tmp_path / "executor_stderr.log"
    verification_log_path = tmp_path / "verification.log"
    stdout_path.write_text(stdout_text, encoding="utf-8")
    stderr_path.write_text(stderr_text, encoding="utf-8")
    verification_log_path.write_text(verification_text, encoding="utf-8")
    return stdout_path, stderr_path, verification_log_path


def _task_contract() -> TaskContract:
    return TaskContract.model_validate(
        {
            "task_id": "fd-0002",
            "release_id": "failure-diagnosis-1",
            "title": "Add failure diagnosis backend seam and prompt builder",
            "task_type": "code_only",
            "budget_class": "M",
            "objective": "Add a replaceable failure-diagnosis backend seam.",
            "allowed_files": ["src/**", "tests/**"],
            "forbidden_changes": ["Do not touch orchestration."],
            "required_evidence": ["git diff", "prompt construction tests", "deterministic backend tests"],
            "verification": {"profile": "default"},
            "stop_conditions": ["The backend interface cannot be tested without a real model call."],
        }
    )
