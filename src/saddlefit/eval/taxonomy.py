"""Failure taxonomy: one primary tag per run, plus secondary tags (B9)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Tag = Literal[
    "env_error",
    "tampered",
    "timeout",
    "context_overflow",
    "malformed_tool_call",
    "loop",
    "gave_up",
    "tool_error",
    "check_fail",
    "solved",
]

# Highest priority first (B9).
PRIORITY: tuple[Tag, ...] = (
    "env_error",
    "tampered",
    "timeout",
    "context_overflow",
    "malformed_tool_call",
    "loop",
    "gave_up",
    "tool_error",
    "check_fail",
)

CONTEXT_OVERFLOW_RATIO = 0.95
MALFORMED_THRESHOLD = 3
TOOL_ERROR_STREAK = 3


@dataclass(frozen=True)
class RunSignals:
    """What the student loop and the evaluator observed about one run."""

    check_passed: bool
    env_error: bool = False
    tampered: bool = False
    hit_step_limit: bool = False
    hit_time_limit: bool = False
    max_prompt_tokens: int = 0
    context_length: int = 0
    context_length_error: bool = False
    malformed_tool_calls: int = 0
    stopped_by_loop_detection: bool = False
    finished: bool = False
    ran_visible_check: bool = False
    diff_empty: bool = False
    max_failing_tool_streak: int = 0


@dataclass(frozen=True)
class Classification:
    primary: Tag
    tags: tuple[Tag, ...]

    @property
    def counts_in_statistics(self) -> bool:
        """``env_error`` runs are excluded from statistics and rerun."""
        return self.primary != "env_error"


def _matches(s: RunSignals) -> list[Tag]:
    overflow = s.context_length_error or (
        s.context_length > 0 and s.max_prompt_tokens >= CONTEXT_OVERFLOW_RATIO * s.context_length
    )
    checks: dict[Tag, bool] = {
        "env_error": s.env_error,
        "tampered": s.tampered,
        "timeout": s.hit_step_limit or s.hit_time_limit,
        "context_overflow": overflow,
        "malformed_tool_call": s.malformed_tool_calls >= MALFORMED_THRESHOLD,
        "loop": s.stopped_by_loop_detection,
        "gave_up": s.finished and (not s.ran_visible_check or s.diff_empty),
        "tool_error": s.max_failing_tool_streak >= TOOL_ERROR_STREAK,
        "check_fail": not s.check_passed,
    }
    return [tag for tag in PRIORITY if checks[tag]]


def classify(signals: RunSignals) -> Classification:
    """Primary tag is the highest-priority match; a passing, untampered run is ``solved``."""
    matched = _matches(signals)
    if signals.check_passed and not (signals.env_error or signals.tampered):
        return Classification(primary="solved", tags=("solved", *matched))
    # Not solved means the check failed, or the run was env_error or tampered: never empty.
    return Classification(primary=matched[0], tags=tuple(matched))
