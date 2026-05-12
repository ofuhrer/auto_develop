from __future__ import annotations

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel


ModelT = TypeVar("ModelT", bound=BaseModel)


def load_yaml_model(path: Path, model_type: type[ModelT]) -> ModelT:
    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file)

    if data is None:
        data = {}

    return model_type.model_validate(data)
