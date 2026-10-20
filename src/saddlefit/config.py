"""Project configuration: ``.saddlefit/saddlefit.yaml`` (B4).

Budgets, limits and evaluation settings live only here; no model can edit this file.
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Final, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

CONFIG_SCHEMA_VERSION: Final = 1
CONFIG_FILENAME = "saddlefit.yaml"


class _Strict(BaseModel):
    """Base for config sections: unknown keys, mutation and non-finite numbers are errors."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class StudentConfig(_Strict):
    """The compact local model that does the tasks."""

    provider: Literal["openai-compatible", "fake", "looped"] = "openai-compatible"
    base_url: str = "http://localhost:11434/v1"
    model: str = "qwen3:8b"
    tool_mode: Literal["auto", "native", "json"] = "auto"
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    seed: int = 7
    context_length: int = Field(default=32768, gt=0)
    release_date: date | None = None


class TokenPrices(_Strict):
    """Tutor prices in USD per million tokens; filled in by the user, never hardcoded."""

    input: float = Field(default=0.0, ge=0.0)
    output: float = Field(default=0.0, ge=0.0)


class TutorConfig(_Strict):
    """The strong model that diagnoses failures, and its per-round budget."""

    provider: Literal["anthropic", "fake"] = "anthropic"
    model: str = "claude-opus-5-5"
    max_calls_per_round: int = Field(default=20, gt=0)
    max_usd_per_round: float = Field(default=5.0, ge=0.0)
    prices_usd_per_mtok: TokenPrices = TokenPrices()


class GeneratorConfig(_Strict):
    """The strong model that writes new tasks; a separate role from the tutor."""

    provider: Literal["anthropic", "fake"] = "anthropic"
    model: str = "claude-fable-5-1"


class EvaluationConfig(_Strict):
    """Repeated-run evaluation settings and paths the student must not touch."""

    runs_per_task: int = Field(default=3, gt=0)
    solved_threshold: int = Field(default=2, gt=0)
    timeout_seconds: int = Field(default=900, gt=0)
    protected_paths: tuple[str, ...] = ("tests/**", "**/test_*.py", "**/*_test.py")

    @model_validator(mode="after")
    def _threshold_fits(self) -> Self:
        """Reject a threshold that no number of passing runs can reach."""
        if self.solved_threshold > self.runs_per_task:
            raise ValueError(
                f"solved_threshold ({self.solved_threshold}) cannot exceed "
                f"runs_per_task ({self.runs_per_task})"
            )
        return self


class SplitsConfig(_Strict):
    """Fractions of the task suite assigned to each split."""

    train: float = Field(default=0.5, ge=0.0, le=1.0)
    held_out: float = Field(default=0.25, ge=0.0, le=1.0)
    control: float = Field(default=0.25, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _fractions_sum_to_one(self) -> Self:
        """Reject fractions that do not cover the suite exactly once."""
        total = self.train + self.held_out + self.control
        if not math.isclose(total, 1.0, abs_tol=1e-9):
            raise ValueError(f"split fractions must sum to 1.0, got {total}")
        return self


class SandboxConfig(_Strict):
    """Image and resource limits for the student and evaluation containers."""

    image: str = "ghcr.io/padobrik/saddlefit-sandbox:0.1.0"
    cpus: float = Field(default=2, gt=0)
    memory: str = Field(default="4g", pattern=r"^[0-9]+[bkmg]?$")
    pids_limit: int = Field(default=512, gt=0)


class FastLoopConfig(_Strict):
    """Budget for the fast loop during a task."""

    max_variants: int = Field(default=3, gt=0)
    runs_per_variant: int = Field(default=2, gt=0)
    time_budget_minutes: float = Field(default=10, gt=0)


class Config(_Strict):
    """The whole ``saddlefit.yaml`` file."""

    schema_version: Literal[1] = CONFIG_SCHEMA_VERSION
    project: str = Field(min_length=1)
    student: StudentConfig = StudentConfig()
    tutor: TutorConfig = TutorConfig()
    generator: GeneratorConfig = GeneratorConfig()
    evaluation: EvaluationConfig = EvaluationConfig()
    splits: SplitsConfig = SplitsConfig()
    sandbox: SandboxConfig = SandboxConfig()
    fast_loop: FastLoopConfig = FastLoopConfig()


class ConfigError(Exception):
    """The configuration file is missing, unreadable or invalid (a user error)."""


def _format_errors(path: Path, exc: ValidationError) -> str:
    """Render validation errors with dotted paths, naming unknown keys explicitly."""
    lines = [f"invalid configuration in {path}:"]
    for err in exc.errors():
        where = ".".join(str(p) for p in err["loc"]) or "<root>"
        if err["type"] == "extra_forbidden":
            lines.append(f"  {where}: unknown key")
        else:
            lines.append(f"  {where}: {err['msg']}")
    return "\n".join(lines)


def load_config(path: Path) -> Config:
    """Read and validate a configuration file; unknown keys are errors."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration not found: {path} (run `saddlefit init`)") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"configuration is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read configuration {path}: {exc.strerror or exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"configuration is not valid YAML: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"configuration must be a mapping: {path}")
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_errors(path, exc)) from exc


def dump_config(config: Config) -> str:
    """Serialize a configuration as YAML in field order."""
    data = config.model_dump(mode="json")
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
