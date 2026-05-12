from __future__ import annotations

import json
import shutil
from pathlib import Path

from agentic_devloop.git_finalize import FinalizeResult
from agentic_devloop.git_state import changed_files, diff_patch
from agentic_devloop.models import EvidenceBundle, ExecutorResult, ReviewDecision, TaskContract, TaskRun


class EvidenceCollector:
    def collect(
        self,
        *,
        run_id: str,
        task: TaskContract,
        run_state: TaskRun,
        worktree_path: Path,
        bundle_path: Path,
        contract_source_path: Path,
        executor_prompt_path: Path,
        executor_result: ExecutorResult,
        verification_log_path: Path,
    ) -> EvidenceBundle:
        if bundle_path.exists() and any(bundle_path.iterdir()):
            raise FileExistsError(f"evidence bundle already exists: {bundle_path}")

        bundle_path.mkdir(parents=True, exist_ok=False)

        contract_path = bundle_path / "contract.yaml"
        run_state_path = bundle_path / "run_state.json"
        prompt_path = bundle_path / "executor_prompt.md"
        executor_stdout_path = bundle_path / "executor_stdout.log"
        executor_stderr_path = bundle_path / "executor_stderr.log"
        git_diff_path = bundle_path / "git_diff.patch"
        changed_files_path = bundle_path / "changed_files.txt"
        verification_bundle_log_path = bundle_path / "verification.log"
        model_call_metadata_path = bundle_path / "model_call_metadata.json"

        shutil.copyfile(contract_source_path, contract_path)
        shutil.copyfile(executor_prompt_path, prompt_path)
        shutil.copyfile(executor_result.stdout_path, executor_stdout_path)
        shutil.copyfile(executor_result.stderr_path, executor_stderr_path)
        shutil.copyfile(verification_log_path, verification_bundle_log_path)

        run_state_path.write_text(
            json.dumps(run_state.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        model_call_metadata_path.write_text(
            json.dumps(
                {
                    "backend": executor_result.backend,
                    "model": executor_result.model,
                    "command": executor_result.command,
                    "exit_code": executor_result.exit_code,
                    "duration_seconds": executor_result.duration_seconds,
                    "timed_out": executor_result.timed_out,
                    "prompt_chars": executor_result.prompt_chars,
                    "stdout_chars": executor_result.stdout_chars,
                    "stderr_chars": executor_result.stderr_chars,
                    "approx_output_chars": executor_result.stdout_chars + executor_result.stderr_chars,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        git_diff_path.write_text(diff_patch(worktree_path), encoding="utf-8")
        changed_files_path.write_text(
            "\n".join(changed_files(worktree_path)) + "\n",
            encoding="utf-8",
        )

        return EvidenceBundle(
            task_id=task.task_id,
            run_id=run_id,
            bundle_path=bundle_path,
            contract_path=contract_path,
            run_state_path=run_state_path,
            executor_prompt_path=prompt_path,
            executor_stdout_path=executor_stdout_path,
            executor_stderr_path=executor_stderr_path,
            git_diff_path=git_diff_path,
            changed_files_path=changed_files_path,
            verification_log_path=verification_bundle_log_path,
            model_call_metadata_path=model_call_metadata_path,
        )


def write_review_decision(bundle: EvidenceBundle, decision: ReviewDecision) -> EvidenceBundle:
    decision_path = bundle.bundle_path / "decision.yaml"
    review_path = bundle.bundle_path / "review.md"

    decision_path.write_text(
        _decision_yaml(decision),
        encoding="utf-8",
    )
    review_path.write_text(
        "\n".join(
            [
                f"# Review: {decision.task_id}",
                "",
                f"- Decision: `{decision.decision}`",
                f"- Reviewer: `{decision.reviewer}`",
                f"- Rationale: {decision.rationale}",
                "",
                "## Risks",
                "",
                *[f"- {risk}" for risk in decision.risks],
                *(["- None recorded."] if not decision.risks else []),
                "",
                "## Follow-up Tasks",
                "",
                *[f"- {task}" for task in decision.follow_up_tasks],
                *(["- None recorded."] if not decision.follow_up_tasks else []),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return bundle.model_copy(update={"decision_path": decision_path, "review_path": review_path})


def write_finalization_result(bundle: EvidenceBundle, result: FinalizeResult) -> EvidenceBundle:
    finalization_path = bundle.bundle_path / "finalization.yaml"
    finalization_path.write_text(
        _yaml(result.__dict__),
        encoding="utf-8",
    )
    return bundle.model_copy(update={"finalization_path": finalization_path})


def _decision_yaml(decision: ReviewDecision) -> str:
    return _yaml(decision.model_dump(mode="json"))


def _yaml(data: dict) -> str:
    import yaml

    return yaml.safe_dump(data, sort_keys=False)
