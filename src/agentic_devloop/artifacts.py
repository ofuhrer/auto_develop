from __future__ import annotations

from pathlib import Path

from agentic_devloop.process import run_process


def cleanup_task_artifacts(
    *,
    repo_path: Path,
    worktree_path: Path,
    branch: str,
    preserve_worktree: bool = False,
    preserve_branch: bool = False,
) -> list[str]:
    messages: list[str] = []
    if preserve_worktree:
        messages.append(f"preserved_worktree={worktree_path}")
    elif worktree_path.exists():
        result = run_process(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_path,
            timeout_seconds=120,
        )
        if result.exit_code == 0:
            messages.append(f"removed_worktree={worktree_path}")
        else:
            messages.append(f"cleanup_worktree_failed={result.stderr.strip() or result.stdout.strip()}")

    if preserve_branch:
        messages.append(f"preserved_branch={branch}")
    else:
        branch_result = run_process(
            ["git", "branch", "-D", branch],
            cwd=repo_path,
            timeout_seconds=120,
        )
        if branch_result.exit_code == 0:
            messages.append(f"deleted_branch={branch}")
        elif "not found" not in branch_result.stderr.lower():
            messages.append(
                f"cleanup_branch_failed={branch_result.stderr.strip() or branch_result.stdout.strip()}"
            )
    return messages
