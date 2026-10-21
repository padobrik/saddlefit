"""Decision rules of the middle loop: batch gate, held-out gate and tie-break (B12).

Each rule works on per-task outcomes under one harness: ``True`` for solved, ``False`` for
unsolved, ``None`` for inconclusive (environment errors left too few runs to decide it).
Both harnesses are compared on the same tasks and the same seeds.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import Final

Outcomes = Mapping[str, bool | None]

HELD_OUT_MARGIN: Final = 0.1
HELD_OUT_MAX_LOSSES: Final = 1


def _check_same_tasks(current: Outcomes, candidate: Outcomes) -> None:
    """Both harnesses must be judged on the same tasks, or a comparison means nothing."""
    if current.keys() != candidate.keys():
        missing = sorted(current.keys() ^ candidate.keys())
        raise ValueError(f"harnesses were run on different tasks: {missing}")


def batch_gate(current: Outcomes, candidate: Outcomes) -> bool:
    """Pass when the candidate solves strictly more batch tasks than the current harness."""
    _check_same_tasks(current, candidate)
    current_solved = sum(outcome is True for outcome in current.values())
    candidate_solved = sum(outcome is True for outcome in candidate.values())
    return candidate_solved > current_solved


@dataclass(frozen=True)
class HeldOutComparison:
    """Per-task wins (unsolved → solved) and losses (solved → unsolved) over n tasks."""

    wins: int
    losses: int
    tasks: int

    @property
    def net(self) -> int:
        return self.wins - self.losses


def compare_held_out(current: Outcomes, candidate: Outcomes) -> HeldOutComparison:
    """Count wins and losses; a task inconclusive under either harness is neither."""
    _check_same_tasks(current, candidate)
    wins = losses = 0
    for task, before in current.items():
        after = candidate[task]
        if before is False and after is True:
            wins += 1
        elif before is True and after is False:
            losses += 1
    return HeldOutComparison(wins=wins, losses=losses, tasks=len(current))


def required_net(tasks: int, margin: float = HELD_OUT_MARGIN) -> int:
    """``max(1, ⌈margin · n⌉)``, computed exactly: 0.07 · 100 in floats rounds up to 8."""
    return max(1, math.ceil(Fraction(str(margin)) * tasks))


def held_out_gate(
    comparison: HeldOutComparison,
    margin: float = HELD_OUT_MARGIN,
    max_losses: int = HELD_OUT_MAX_LOSSES,
) -> bool:
    """Pass when ``wins - losses ≥ max(1, ⌈margin · n⌉)`` and ``losses ≤ max_losses``."""
    return comparison.net >= required_net(comparison.tasks, margin) and (
        comparison.losses <= max_losses
    )


@dataclass(frozen=True)
class Candidate:
    """A change that passed both gates; ``order`` is when its proposal was made."""

    proposal_id: str
    order: int
    held_out: HeldOutComparison
    context_tokens_added: int


def tie_break(candidates: Iterable[Candidate]) -> Candidate | None:
    """Larger ``wins - losses``, then fewer added context tokens, then the earlier proposal."""
    return min(
        candidates,
        key=lambda c: (-c.held_out.net, c.context_tokens_added, c.order),
        default=None,
    )
