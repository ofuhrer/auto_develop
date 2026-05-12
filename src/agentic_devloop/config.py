from __future__ import annotations

from pathlib import Path
import shlex

from agentic_devloop.models import ProjectConfig
from agentic_devloop.yaml_io import load_yaml_model


class ProjectConfigError(ValueError):
    pass


def project_config_path(config_dir: Path, project_id: str) -> Path:
    return config_dir / f"{project_id}.yaml"


def load_project_config(
    project_id: str,
    config_dir: Path = Path("configs"),
    *,
    validate_repo: bool = False,
) -> ProjectConfig:
    path = project_config_path(config_dir, project_id)
    if not path.exists():
        raise ProjectConfigError(f"project config not found: {path}")

    config = load_yaml_model(path, ProjectConfig)
    if config.project_id != project_id:
        raise ProjectConfigError(
            f"project config id mismatch: expected {project_id!r}, got {config.project_id!r}"
        )

    if validate_repo and not config.repo_path.exists():
        raise ProjectConfigError(f"repo path does not exist: {config.repo_path}")

    return _resolve_controller_paths(config, path)


def _resolve_controller_paths(config: ProjectConfig, config_path: Path) -> ProjectConfig:
    if config.repo_state_path is None or config.repo_state_path.is_absolute():
        return config

    controller_root = config_path.resolve().parent.parent
    controller_candidate = controller_root / config.repo_state_path
    target_candidate = config.repo_path.resolve() / config.repo_state_path
    if controller_candidate.exists() or not target_candidate.exists():
        repo_state_path = controller_candidate
    else:
        repo_state_path = target_candidate
    return config.model_copy(update={"repo_state_path": repo_state_path})


def discover_safe_verification_runtime(config: ProjectConfig | None) -> str | None:
    if config is None:
        return None
    for profile in config.verification_profiles.values():
        for command in profile.commands:
            for token in shlex.split(command):
                if not token.startswith("/"):
                    continue
                if token.endswith("/bin/python") or token.endswith("/bin/python3"):
                    return token
    return None
