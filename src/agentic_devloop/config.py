from __future__ import annotations

from pathlib import Path

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

    return config
