"""``loop.yaml``: the harness's loop behaviour, and its clamping to ``saddlefit.yaml`` (B4, B6).

The tutor may edit ``loop.yaml``, so it holds behaviour only. Budgets and limits live in
``saddlefit.yaml``; every value here is clamped to them before a run uses it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from saddlefit.config import Config
from saddlefit.errors import UserError
from saddlefit.storage.migrations import MigrationError, upgrade
from saddlefit.storage.migrations.loop import LOOP_KIND
from saddlefit.yamlio import safe_load_unique

LOOP_SCHEMA_VERSION: Final = 2
# The tools a harness may switch off; finish is always on, or no run could end.
OPTIONAL_TOOLS: Final = ("read_file", "list_dir", "search", "edit_file", "write_file", "bash")
MAX_HOOKS: Final = 4
# One step takes at least this long, so max_steps cannot outlast evaluation.timeout_seconds.
MIN_SECONDS_PER_STEP: Final = 1


class _Strict(BaseModel):
    # strict: loop.yaml must match its JSON Schema exactly; "40" is not an integer here
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, strict=True)


class ToolOutputConfig(_Strict):
    """How much of each tool's output the student sees."""

    max_chars: int = Field(default=4000, gt=0)


class ContextConfig(_Strict):
    """Token budgets for the whole prompt and for the harness share of it.

    ``repo_map`` adds the repository's file list to the task message at the first step.
    """

    budget_tokens: int = Field(default=24000, gt=0)
    harness_budget_tokens: int = Field(default=4000, gt=0)
    repo_map: bool = False


class ToolsConfig(_Strict):
    """Which tools the student gets, and the shape of two of them.

    ``read_line_numbers`` prefixes ``read_file`` output with line numbers, as ``cat -n``
    does. ``edit_strips_line_numbers`` lets ``edit_file`` take ``old`` text the student
    copied with those prefixes: when every line of ``old`` has one, they are removed from
    ``old`` and from ``new`` before matching.
    """

    # a YAML list is read as a tuple; the items stay strictly strings
    enabled: tuple[str, ...] = Field(default=OPTIONAL_TOOLS, strict=False)
    read_line_numbers: bool = True
    edit_strips_line_numbers: bool = False

    @model_validator(mode="after")
    def _known_tools(self) -> Self:
        unknown = sorted(set(self.enabled) - set(OPTIONAL_TOOLS))
        if unknown:
            raise ValueError(f"unknown or always-on tools in tools.enabled: {unknown}")
        return self


class HookConfig(_Strict):
    """One fixed step the loop adds; it runs in the sandbox like any tool call.

    ``event``: ``post_tool`` runs after a successful call of one of ``tools`` (any tool when empty);
    ``before_finish`` runs when the student calls ``finish``, before the finish gate.
    ``run`` is a shell command whose output the student sees; ``inject`` is a fixed note.
    """

    # not "on": YAML 1.1 reads a bare on as true
    event: Literal["post_tool", "before_finish"]
    tools: tuple[str, ...] = Field(default=(), strict=False)
    run: str | None = Field(default=None, min_length=1)
    inject: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _one_action(self) -> Self:
        if (self.run is None) == (self.inject is None):
            raise ValueError("a hook needs exactly one of run or inject")
        if self.tools and self.event != "post_tool":
            raise ValueError("only a post_tool hook can name tools")
        unknown = sorted(set(self.tools) - set(OPTIONAL_TOOLS))
        if unknown:
            raise ValueError(f"unknown tools in a hook: {unknown}")
        return self


class FinishGateConfig(_Strict):
    """What runs when the student calls ``finish``."""

    reread_task: bool = True
    run: str | None = "pytest -q"
    max_retries: int = Field(default=1, ge=0)


class LoopDetectionConfig(_Strict):
    """Stop after this many identical calls with no workspace change."""

    identical_calls: int = Field(default=3, ge=2)


class ReasoningConfig(_Strict):
    """How much the student reasons; sent to the runner with every request."""

    effort: Literal["off", "low", "high"] = "low"


class LoopConfig(_Strict):
    """The whole ``loop.yaml`` file."""

    schema_version: Literal[2]
    max_steps: int = Field(default=40, gt=0)
    tool_output: ToolOutputConfig = ToolOutputConfig()
    context: ContextConfig = ContextConfig()
    finish_gate: FinishGateConfig = FinishGateConfig()
    loop_detection: LoopDetectionConfig = LoopDetectionConfig()
    reasoning: ReasoningConfig = ReasoningConfig()
    tools: ToolsConfig = ToolsConfig()
    hooks: tuple[HookConfig, ...] = Field(default=(), max_length=MAX_HOOKS, strict=False)


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
        raw = upgrade(LOOP_KIND, raw, LOOP_SCHEMA_VERSION)
    except MigrationError as exc:
        raise LoopConfigError(f"invalid {path}: {exc}") from exc
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
            "context": loop.context.model_copy(
                update={"budget_tokens": budget, "harness_budget_tokens": harness_budget}
            ),
        }
    )
    return LoopConfig.model_validate(clamped.model_dump()), notes
