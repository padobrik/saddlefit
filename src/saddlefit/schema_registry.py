"""Persisted models whose JSON Schemas are generated into ``schemas/``."""

from __future__ import annotations

from pydantic import BaseModel

# name -> model; the file is written as schemas/<name>.schema.json
SCHEMA_MODELS: dict[str, type[BaseModel]] = {}
