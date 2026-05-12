from __future__ import annotations

import json
import shutil
from pathlib import Path

from agentic_devloop.models import EvidenceBundle, ExecutorResult, TaskContract, TaskRun
from agentic_devloop.process import run_process


def _git_text(worktree_path: Path, args: list[str]) -> str:
    result = run_process(["git", *args], cwd=worktree_path, timeout_seconds=60)
    if result.exit_code != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


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

        shutil.copyfile(contract_source_path, contract_path)
        shutil.copyfile(executor_prompt_path, prompt_path)
        shutil.copyfile(executor_result.stdout_path, executor_stdout_path)
        shutil.copyfile(executor_result.stderr_path, executor_stderr_path)
        shutil.copyfile(verification_log_path, verification_bundle_log_path)

        run_state_path.write_text(
            json.dumps(run_state.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        git_diff_path.write_text(_git_text(worktree_path, ["diff", "--patch"]), encoding="utf-8")
        changed_files_path.write_text(
            _git_text(worktree_path, ["diff", "--name-only"]),
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
        )
