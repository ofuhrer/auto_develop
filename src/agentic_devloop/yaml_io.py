from __future__ import annotations

from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel


ModelT = TypeVar("ModelT", bound=BaseModel)


def load_yaml_model(path: Path, model_type: type[ModelT]) -> ModelT:
    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file)

    if data is None:
        data = {}

    return model_type.model_validate(data)


def dump_yaml_data(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False)


def write_yaml_data(path: Path, data: Any, *, overwrite: bool = False) -> Path:
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing YAML file: {path}")
    path.write_text(dump_yaml_data(data), encoding="utf-8")
    return path


def write_yaml_model(path: Path, model: BaseModel, *, overwrite: bool = False) -> Path:
    return write_yaml_data(path, model.model_dump(mode="json"), overwrite=overwrite)
