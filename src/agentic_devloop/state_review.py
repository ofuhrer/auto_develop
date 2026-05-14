from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from agentic_devloop.models import ReleaseObjective, StateRefreshSummary, StateReviewSnapshot
from agentic_devloop.process import run_process
from agentic_devloop.yaml_io import load_yaml_model


def write_state_review_context_bundle(
    *,
    snapshot: StateReviewSnapshot,
    state_review_snapshot_path: Path,
    runs_dir: Path,
    artifacts_dir: Path,
    objective_path: Path | None = None,
    max_chars: int = 50_000,
    now: datetime | None = None,
) -> tuple[Path, Path]:
    """Persist a state-review-phase context bundle + manifest next to snapshot artifacts."""

    artifacts_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = artifacts_dir / "state_review_context_bundle.md"
    manifest_path = artifacts_dir / "state_review_context_manifest.json"

    included_categories: list[str] = []
    omitted_categories: list[str] = []
    truncation_records: list[dict[str, object]] = []
    artifact_paths: dict[str, list[str]] = {}

    remaining = max(0, int(max_chars))
    sections: list[str] = []

    def append_category(*, name: str, content: str, sources: list[Path]) -> None:
        nonlocal remaining
        if remaining <= 0:
            omitted_categories.append(name)
            return
        if len(content) <= remaining:
            sections.append(content)
            remaining -= len(content)
            included_categories.append(name)
            artifact_paths[name] = [str(path.resolve()) for path in sources]
            return
        included = content[:remaining]
        sections.append(included)
        truncation_records.append(
            {
                "category": name,
                "source_path": str(sources[0].resolve()) if sources else None,
                "original_chars": len(content),
                "included_chars": len(included),
                "omitted_chars": len(content) - len(included),
            }
        )
        remaining = 0
        included_categories.append(name)
        artifact_paths[name] = [str(path.resolve()) for path in sources]

    created_at = now or datetime.now(UTC)
    objective: ReleaseObjective | None = None
    if objective_path is not None and objective_path.exists():
        objective = load_yaml_model(objective_path, ReleaseObjective)
        objective_payload = json.dumps(objective.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        append_category(
            name="objective",
            content=(
                "\n".join(
                    [
                        "## Release Objective",
                        "",
                        f"objective_path={objective_path.resolve()}",
                        "",
                        f"```json\n{objective_payload}```",
                        "",
                    ]
                )
            ),
            sources=[objective_path],
        )
    else:
        omitted_categories.append("objective")

    snapshot_payload = json.dumps(snapshot.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    append_category(
        name="state_review_snapshot",
        content=(
            "\n".join(
                [
                    "## State Review Snapshot",
                    "",
                    f"snapshot_path={state_review_snapshot_path.resolve()}",
                    "",
                    f"```json\n{snapshot_payload}```",
                    "",
                ]
            )
        ),
        sources=[state_review_snapshot_path],
    )

    repo_state_paths: list[Path] = []
    for key in (
        "architecture_summary",
        "active_constraints",
        "backlog_state",
        "release_plan",
        "benchmark_status",
    ):
        raw_path = snapshot.repo_state_files.get(key)
        if isinstance(raw_path, str) and raw_path.strip():
            repo_state_paths.append(Path(raw_path))

    if repo_state_paths:
        pieces: list[str] = []
        per_file_cap = 12_000
        for path in repo_state_paths:
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            snippet = content
            if len(snippet) > per_file_cap:
                snippet = snippet[:per_file_cap]
                truncation_records.append(
                    {
                        "category": "repo_state_memory",
                        "source_path": str(path.resolve()),
                        "original_chars": len(content),
                        "included_chars": len(snippet),
                        "omitted_chars": len(content) - len(snippet),
                    }
                )
            pieces.append(f"### {path.name}\n\n```\n{snippet.rstrip()}\n```\n")
        if pieces:
            append_category(
                name="repo_state_memory",
                content=("\n".join(["## Repo-State Memory", "", "\n".join(pieces).rstrip(), ""]) + "\n"),
                sources=repo_state_paths,
            )
        else:
            omitted_categories.append("repo_state_memory")
    else:
        omitted_categories.append("repo_state_memory")

    metric_paths: list[Path] = []
    finding_paths: list[Path] = []
    for run_name in list(snapshot.recent_release_runs)[:3]:
        run_dir = runs_dir / run_name
        if not run_dir.is_dir():
            continue
        metrics = run_dir / "release_metrics.json"
        if metrics.exists():
            metric_paths.append(metrics)
        for candidate in (
            run_dir / "release_review.md",
            run_dir / "feature_review.json",
            run_dir / "feature_review_recheck.json",
        ):
            if candidate.exists():
                finding_paths.append(candidate)

    if metric_paths:
        pieces: list[str] = []
        for path in metric_paths:
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            pieces.append(f"### {path.parent.name}/{path.name}\n\n```json\n{content.rstrip()}\n```\n")
        if pieces:
            append_category(
                name="recent_metrics",
                content=("\n".join(["## Recent Metrics", "", "\n".join(pieces).rstrip(), ""]) + "\n"),
                sources=metric_paths,
            )
        else:
            omitted_categories.append("recent_metrics")
    else:
        omitted_categories.append("recent_metrics")

    if finding_paths:
        pieces: list[str] = []
        per_file_cap = 10_000
        for path in finding_paths:
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            snippet = content
            if len(snippet) > per_file_cap:
                snippet = snippet[:per_file_cap]
                truncation_records.append(
                    {
                        "category": "prior_findings",
                        "source_path": str(path.resolve()),
                        "original_chars": len(content),
                        "included_chars": len(snippet),
                        "omitted_chars": len(content) - len(snippet),
                    }
                )
            fence = "json" if path.suffix == ".json" else ""
            pieces.append(f"### {path.parent.name}/{path.name}\n\n```{fence}\n{snippet.rstrip()}\n```\n")
        if pieces:
            append_category(
                name="prior_findings",
                content=("\n".join(["## Prior Findings", "", "\n".join(pieces).rstrip(), ""]) + "\n"),
                sources=finding_paths,
            )
        else:
            omitted_categories.append("prior_findings")
    else:
        omitted_categories.append("prior_findings")

    bundle_text = "\n".join(["# State-Review Context Bundle", "", *sections]).rstrip() + "\n"
    bundle_path.write_text(bundle_text, encoding="utf-8")

    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "phase": "state_review",
                "created_at": created_at.isoformat().replace("+00:00", "Z"),
                "created_by": "state_review",
                "release_id": objective.release_id if objective is not None else None,
                "max_chars": int(max_chars),
                "included_categories": included_categories,
                "omitted_categories": sorted(set(omitted_categories)),
                "total_chars_included": len(bundle_text),
                "total_chars_omitted": sum(int(record.get("omitted_chars", 0)) for record in truncation_records),
                "truncation_records": truncation_records,
                "artifact_paths": artifact_paths,
                "bundle_path": str(bundle_path.resolve()),
                "state_review_snapshot_path": str(state_review_snapshot_path.resolve()),
                "objective_path": str(objective_path.resolve()) if objective_path is not None else None,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return bundle_path, manifest_path


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


def build_state_refresh_summary(
    *,
    snapshot: StateReviewSnapshot,
    state_review_snapshot_path: Path,
) -> StateRefreshSummary:
    tracked_repo_state_file_count = sum(1 for value in snapshot.repo_state_files.values() if value is not None)
    return StateRefreshSummary(
        captured_at=snapshot.captured_at,
        state_review_snapshot_path=state_review_snapshot_path,
        branch=snapshot.branch,
        head_commit=snapshot.head_commit,
        status_count=len(snapshot.status_lines),
        local_branch_count=len(snapshot.local_branches),
        worktree_count=len(snapshot.worktrees),
        repo_state_file_count=tracked_repo_state_file_count,
        recent_release_run_count=len(snapshot.recent_release_runs),
    )


def write_state_refresh_summary_artifact(*, summary: StateRefreshSummary, artifacts_dir: Path) -> Path:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifacts_dir / "state_refresh_summary.json"
    artifact_path.write_text(
        json.dumps(summary.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
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
