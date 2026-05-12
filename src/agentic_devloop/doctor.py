from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agentic_devloop.config import load_project_config
from agentic_devloop.models import ExecutorConfig, ProjectConfig
from agentic_devloop.process import run_process


UNSUPPORTED_WORKER_MODELS = {"gpt-5.3-codex-spark"}


@dataclass(frozen=True)
class DoctorDiagnostic:
    check: str
    severity: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
        }


@dataclass(frozen=True)
class DoctorReport:
    project_id: str
    repo_path: Path
    repo_exists: bool
    repo_is_git_repo: bool
    current_branch: str | None
    dirty_files: list[str]
    worktree_root: dict[str, object]
    verification_profiles: dict[str, list[str]]
    model_routing: dict[str, object]
    release: dict[str, object] | None
    diagnostics: list[DoctorDiagnostic] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "repo_path": str(self.repo_path),
            "repo_exists": self.repo_exists,
            "repo_is_git_repo": self.repo_is_git_repo,
            "current_branch": self.current_branch,
            "dirty_files": self.dirty_files,
            "dirty": bool(self.dirty_files),
            "worktree_root": self.worktree_root,
            "verification_profiles": self.verification_profiles,
            "model_routing": self.model_routing,
            "release": self.release,
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }


def run_doctor(
    *,
    project_id: str,
    config_dir: Path = Path("configs"),
    release_id: str | None = None,
) -> DoctorReport:
    config = load_project_config(project_id, config_dir, validate_repo=True)
    diagnostics: list[DoctorDiagnostic] = []

    repo_is_git_repo = _is_git_repo(config.repo_path)
    if not repo_is_git_repo:
        diagnostics.append(
            DoctorDiagnostic(
                check="git",
                severity="warning",
                message="configured repository path exists but is not a Git repository; release preflight cannot inspect branches or dirty state.",
            )
        )
    current_branch = _current_branch(config.repo_path) if repo_is_git_repo else None
    if repo_is_git_repo and current_branch is None:
        diagnostics.append(
            DoctorDiagnostic(
                check="git",
                severity="warning",
                message="repository is in detached HEAD state; release preflight should start from a named branch.",
            )
        )

    dirty_files = _dirty_files(config.repo_path) if repo_is_git_repo else []
    if repo_is_git_repo and dirty_files:
        diagnostics.append(
            DoctorDiagnostic(
                check="git",
                severity="warning",
                message="repository has uncommitted changes: "
                + ", ".join(dirty_files[:5])
                + (" ..." if len(dirty_files) > 5 else ""),
            )
        )

    worktree_entries = _worktree_entries(config.worktree_root)
    if worktree_entries:
        diagnostics.append(
            DoctorDiagnostic(
                check="worktree_root",
                severity="warning",
                message="project worktree root contains stale entries: "
                + ", ".join(str(path) for path in worktree_entries[:5])
                + (f" ... (+{len(worktree_entries) - 5} more)" if len(worktree_entries) > 5 else ""),
            )
        )

    release = None
    if release_id is not None:
        release = _release_branches(config.repo_path, release_id) if repo_is_git_repo else {
            "release_id": release_id,
            "integration_branch": [],
            "task_branches": [],
        }
        if release["integration_branch"]:
            diagnostics.append(
                DoctorDiagnostic(
                    check="release_branch",
                    severity="warning",
                    message="integration branch already exists for this release: "
                    + ", ".join(release["integration_branch"]),
                )
            )
        if release["task_branches"]:
            diagnostics.append(
                DoctorDiagnostic(
                    check="task_branches",
                    severity="warning",
                    message="task branches already exist for this release: "
                    + ", ".join(release["task_branches"][:5])
                    + (f" ... (+{len(release['task_branches']) - 5} more)" if len(release["task_branches"]) > 5 else ""),
                )
            )

    verification_profiles = {
        name: profile.commands for name, profile in config.verification_profiles.items()
    }
    model_routing = _build_model_routing_summary(config)
    diagnostics.extend(_model_routing_diagnostics(config, model_routing["resolved_roles"]))

    return DoctorReport(
        project_id=config.project_id,
        repo_path=config.repo_path,
        repo_exists=config.repo_path.exists(),
        repo_is_git_repo=repo_is_git_repo,
        current_branch=current_branch,
        dirty_files=dirty_files,
        worktree_root={
            "path": str(config.worktree_root),
            "exists": config.worktree_root.exists(),
            "entries": [str(path) for path in worktree_entries],
            "clean": not worktree_entries,
        },
        verification_profiles=verification_profiles,
        model_routing=model_routing,
        release=release,
        diagnostics=diagnostics,
    )


def _build_model_routing_summary(config: ProjectConfig) -> dict[str, object]:
    resolved_roles = _resolved_role_configs(config)
    return {
        "default_role": config.model_routing.default_role,
        "task_type_roles": {key.value: value for key, value in config.model_routing.task_type_roles.items()},
        "budget_class_roles": dict(config.model_routing.budget_class_roles),
        "escalation_role": config.model_routing.escalation_role,
        "resolved_roles": {
            role: {
                "type": executor_config.type,
                "model": executor_config.model,
                "fallback_models": executor_config.fallback_models,
            }
            for role, executor_config in resolved_roles.items()
        },
    }


def _resolved_role_configs(config: ProjectConfig) -> dict[str, ExecutorConfig]:
    roles = {config.model_routing.default_role, *config.model_routing.task_type_roles.values(), *config.model_routing.budget_class_roles.values()}
    if config.model_routing.escalation_role is not None:
        roles.add(config.model_routing.escalation_role)
    return {role: config.model_roles.get(role, config.executor) for role in sorted(roles)}


def _model_routing_diagnostics(
    config: ProjectConfig,
    resolved_roles: dict[str, object],
) -> list[DoctorDiagnostic]:
    diagnostics: list[DoctorDiagnostic] = []
    for role, summary in resolved_roles.items():
        model = str(summary["model"])
        if model in UNSUPPORTED_WORKER_MODELS:
            diagnostics.append(
                DoctorDiagnostic(
                    check="model_routing",
                    severity="warning",
                    message=f"role {role} resolves to known unsupported model {model}. Switch the role to a supported worker primary before release execution.",
                )
            )
        fallback_models = summary["fallback_models"]
        if any(fallback in UNSUPPORTED_WORKER_MODELS for fallback in fallback_models):
            diagnostics.append(
                DoctorDiagnostic(
                    check="model_routing",
                    severity="warning",
                    message=f"role {role} includes known unsupported fallback model(s): {', '.join(fallback for fallback in fallback_models if fallback in UNSUPPORTED_WORKER_MODELS)}",
                )
            )
    return diagnostics


def _is_git_repo(repo_path: Path) -> bool:
    result = run_process(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo_path, timeout_seconds=30)
    return result.exit_code == 0 and result.stdout.strip() == "true"


def _current_branch(repo_path: Path) -> str | None:
    result = run_process(["git", "branch", "--show-current"], cwd=repo_path, timeout_seconds=30)
    if result.exit_code != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


def _dirty_files(repo_path: Path) -> list[str]:
    result = run_process(["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo_path, timeout_seconds=30)
    if result.exit_code != 0:
        return []
    files: list[str] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", maxsplit=1)[1]
        files.append(path)
    return files


def _worktree_entries(worktree_root: Path) -> list[Path]:
    if not worktree_root.exists():
        return []
    return sorted(
        path for path in worktree_root.iterdir() if path.is_dir() and not path.name.startswith(".")
    )


def _release_branches(repo_path: Path, release_id: str) -> dict[str, list[str]]:
    integration_branch = _branch_list(repo_path, f"feature/{release_id}")
    task_branches = _branch_list(repo_path, f"agent/{release_id}/*")
    return {
        "release_id": release_id,
        "integration_branch": integration_branch,
        "task_branches": task_branches,
    }


def _branch_list(repo_path: Path, *patterns: str) -> list[str]:
    result = run_process(["git", "branch", "--list", *patterns], cwd=repo_path, timeout_seconds=30)
    if result.exit_code != 0:
        return []
    branches: list[str] = []
    for line in result.stdout.splitlines():
        branch = line.strip()
        if branch.startswith("* "):
            branch = branch[2:]
        if branch:
            branches.append(branch)
    return branches
