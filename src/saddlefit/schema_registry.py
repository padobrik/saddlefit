"""Persisted models whose JSON Schemas are generated into ``schemas/``."""

from __future__ import annotations

from pydantic import BaseModel

from saddlefit.config import Config
from saddlefit.harness.loop_config import LoopConfig
from saddlefit.tasks.models import SuiteLock, TaskMeta

# name -> model; the file is written as schemas/<name>.schema.json
SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "saddlefit-config": Config,
    "loop-config": LoopConfig,
    "suite-lock": SuiteLock,
    "task-meta": TaskMeta,
}
