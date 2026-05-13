from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from agentic_devloop.models import StateReviewSnapshot
from agentic_devloop.process import run_process


def collect_state_review_snapshot(
    *,
    repo_path: Path,
    repo_state_path: Path | None,
    runs_dir: Path,
    now: datetime | None = None,
) -> StateReviewSnapshot:
    timestamp = now or datetime.now(UTC)
    resolved_repo_state_path = _resolve_repo_state_path(repo_path=repo_path, repo_state_path=repo_state_path)

    branch = _git_stdout(repo_path, ["git", "branch", "--show-current"])
    head_commit = _git_stdout(repo_path, ["git", "rev-parse", "HEAD"])
    status_lines = sorted(_git_lines(repo_path, ["git", "status", "--short"]))
    local_branches = sorted(
        _git_lines(repo_path, ["git", "for-each-ref", "refs/heads", "--format=%(refname:short)"])
    )
    worktree_entries = sorted(_git_worktree_entries(repo_path), key=lambda entry: entry["path"])

    repo_state_files = {
        "architecture_summary": _tracked_path(resolved_repo_state_path, "architecture_summary.md"),
        "active_constraints": _tracked_path(resolved_repo_state_path, "active_constraints.yaml"),
        "backlog_state": _tracked_path(resolved_repo_state_path, "backlog_state.yaml"),
        "release_plan": _tracked_path(resolved_repo_state_path, "release_plan.yaml"),
        "benchmark_status": _tracked_path(resolved_repo_state_path, "benchmark_status.json"),
    }

    recent_run_dirs = sorted(
        [path.name for path in runs_dir.glob("*_release") if path.is_dir()],
        reverse=True,
    )[:10]

    return StateReviewSnapshot(
        captured_at=timestamp,
        repo_path=repo_path,
        repo_state_path=resolved_repo_state_path,
        branch=branch,
        head_commit=head_commit,
        status_lines=status_lines,
        local_branches=local_branches,
        worktrees=worktree_entries,
        repo_state_files=repo_state_files,
        recent_release_runs=recent_run_dirs,
    )


def write_state_review_snapshot_artifact(*, snapshot: StateReviewSnapshot, artifacts_dir: Path) -> Path:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifacts_dir / "state_review_snapshot.json"
    artifact_path.write_text(
        json.dumps(snapshot.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return artifact_path


def _resolve_repo_state_path(*, repo_path: Path, repo_state_path: Path | None) -> Path | None:
    if repo_state_path is None:
        return None
    return repo_state_path if repo_state_path.is_absolute() else repo_path / repo_state_path


def _tracked_path(repo_state_path: Path | None, name: str) -> str | None:
    if repo_state_path is None:
        return None
    return str((repo_state_path / name).resolve())


def _git_stdout(repo_path: Path, command: list[str]) -> str:
    result = run_process(command, cwd=repo_path, timeout_seconds=30)
    if result.exit_code != 0:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or f"command failed: {' '.join(command)}")
    return result.stdout.strip()


def _git_lines(repo_path: Path, command: list[str]) -> list[str]:
    output = _git_stdout(repo_path, command)
    if not output:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def _git_worktree_entries(repo_path: Path) -> list[dict[str, str]]:
    output = _git_stdout(repo_path, ["git", "worktree", "list", "--porcelain"])
    if not output:
        return []

    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line.split(" ", 1)[1]
        elif line.startswith("HEAD "):
            current["head"] = line.split(" ", 1)[1]
        elif line.startswith("branch "):
            current["branch"] = line.split(" ", 1)[1].removeprefix("refs/heads/")
    if current:
        entries.append(current)
    return entries
