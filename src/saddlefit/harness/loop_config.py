"""``loop.yaml``: the harness's loop behaviour, and its clamping to ``saddlefit.yaml`` (B4, B6).

The tutor may edit ``loop.yaml``, so it holds behaviour only. Budgets and limits live in
``saddlefit.yaml``; every value here is clamped to them before a run uses it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from saddlefit.config import Config
from saddlefit.errors import UserError
from saddlefit.yamlio import safe_load_unique

LOOP_SCHEMA_VERSION: Final = 1
# One step takes at least this long, so max_steps cannot outlast evaluation.timeout_seconds.
MIN_SECONDS_PER_STEP: Final = 1


class _Strict(BaseModel):
    # strict: loop.yaml must match its JSON Schema exactly; "40" is not an integer here
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, strict=True)


class ToolOutputConfig(_Strict):
    """How much of each tool's output the student sees."""

    max_chars: int = Field(default=4000, gt=0)


class ContextConfig(_Strict):
    """Token budgets for the whole prompt and for the harness share of it."""

    budget_tokens: int = Field(default=24000, gt=0)
    harness_budget_tokens: int = Field(default=4000, gt=0)


class FinishGateConfig(_Strict):
    """What runs when the student calls ``finish``."""

    reread_task: bool = True
    run: str | None = "pytest -q"
    max_retries: int = Field(default=1, ge=0)


class LoopDetectionConfig(_Strict):
    """Stop after this many identical calls with no workspace change."""

    identical_calls: int = Field(default=3, ge=2)


class ReasoningConfig(_Strict):
    """Reasoning language, amount and latent depth (looped models only)."""

    language: Literal["english", "user", "mixed"] = "mixed"
    effort: Literal["off", "low", "high"] = "low"
    latent_depth: int | None = Field(default=None, ge=1)


class LoopConfig(_Strict):
    """The whole ``loop.yaml`` file."""

    schema_version: Literal[1]
    max_steps: int = Field(default=40, gt=0)
    tool_output: ToolOutputConfig = ToolOutputConfig()
    context: ContextConfig = ContextConfig()
    finish_gate: FinishGateConfig = FinishGateConfig()
    loop_detection: LoopDetectionConfig = LoopDetectionConfig()
    reasoning: ReasoningConfig = ReasoningConfig()


class LoopConfigError(UserError):
    """``loop.yaml`` is missing or invalid."""


def default_loop_config() -> LoopConfig:
    """The B6 example values: what a fresh harness store starts with."""
    return LoopConfig(schema_version=LOOP_SCHEMA_VERSION)


def load_loop_config(path: Path) -> LoopConfig:
    """Read and validate ``loop.yaml``; strict types, no duplicate or unknown keys."""
    try:
        raw = safe_load_unique(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise LoopConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise LoopConfigError(f"{path} must be a mapping")
    try:
        return LoopConfig.model_validate(raw)
    except ValidationError as exc:
        raise LoopConfigError(f"invalid {path}: {exc}") from exc


def dump_loop_config(loop: LoopConfig) -> str:
    """Serialize ``loop.yaml`` in field order."""
    return yaml.safe_dump(loop.model_dump(mode="json"), sort_keys=False)


def clamp(loop: LoopConfig, config: Config) -> tuple[LoopConfig, list[str]]:
    """Clamp ``loop`` to the limits in ``saddlefit.yaml``; return it and what was changed."""
    notes: list[str] = []

    def limit(name: str, value: int, ceiling: int) -> int:
        """Cap ``value`` at ``ceiling`` and note the change."""
        if value > ceiling:
            notes.append(f"{name} {value} clamped to {ceiling}")
            return ceiling
        return value

    max_steps = limit(
        "max_steps", loop.max_steps, config.evaluation.timeout_seconds // MIN_SECONDS_PER_STEP
    )
    budget = limit(
        "context.budget_tokens", loop.context.budget_tokens, config.student.context_length
    )
    harness_budget = limit(
        "context.harness_budget_tokens", loop.context.harness_budget_tokens, budget
    )
    clamped = loop.model_copy(
        update={
            "max_steps": max_steps,
            "context": ContextConfig(budget_tokens=budget, harness_budget_tokens=harness_budget),
        }
    )
    return LoopConfig.model_validate(clamped.model_dump()), notes
