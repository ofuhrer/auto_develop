from __future__ import annotations

from pathlib import Path

import pytest

from agentic_devloop.config import ProjectConfigError, load_project_config


ROOT = Path(__file__).resolve().parents[1]


def test_load_project_config() -> None:
    config = load_project_config("rust_rockfall", ROOT / "configs")

    assert config.project_id == "rust_rockfall"
    assert config.default_base_branch == "main"


def test_missing_repo_fails_when_repo_validation_is_requested() -> None:
    with pytest.raises(ProjectConfigError, match="repo path does not exist"):
        load_project_config("rust_rockfall", ROOT / "configs", validate_repo=True)
