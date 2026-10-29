"""Decision rules of the middle loop: batch gate, held-out gate and tie-break (B12).

The batch gate works on per-task outcomes under one harness: ``True`` for solved, ``False``
for unsolved, ``None`` for inconclusive (environment errors left too few runs to decide it).
The held-out gate and ablation work on passing runs per task, compared in pairs: a task
that passes "sometimes" flips between solved and unsolved however many runs decide it, so
counting flips accepts noise (a third of no-change candidates on six tasks). Both harnesses
are compared on the same tasks and the same seeds.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import Final

from saddlefit.eval.statistics import paired_sign_flip_p

Outcomes = Mapping[str, bool | None]
Passes = Mapping[str, int]  # passing runs per task, for tasks with every run counted

HELD_OUT_ALPHA: Final = 0.05  # for one candidate; a round divides it by its candidates
ABLATION_ALPHA: Final = 0.05
MIN_GATE_RUNS: Final = 3  # one run per task is a smoke setting, not a gate
# below this, a paired test on held-out finds only large gains (most changes fail the gate)
ADVISED_HELD_OUT: Final = 20


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
    """Per-task wins (unsolved → solved) and losses (solved → unsolved) over n tasks, and the
    differences in passing runs (candidate minus current) the gates decide on.

    ``diffs`` carry no task IDs: what reaches the tutor and the records are counts only.
    """

    wins: int
    losses: int
    tasks: int
    # tasks decided under both harnesses; None when the count came as an aggregate without it
    compared: int | None = None
    diffs: tuple[int, ...] = ()

    @property
    def net(self) -> int:
        return self.wins - self.losses

    @property
    def net_passes(self) -> int:
        return sum(self.diffs)

    @cached_property
    def p_value(self) -> float:
        """One-sided: how likely a gain this large is from a change that does nothing."""
        return paired_sign_flip_p(self.diffs)

    @property
    def conclusive(self) -> bool:
        return (self.tasks if self.compared is None else self.compared) > 0


def compare_held_out(
    current: Outcomes,
    candidate: Outcomes,
    current_passes: Passes | None = None,
    candidate_passes: Passes | None = None,
) -> HeldOutComparison:
    """Count wins and losses, and the pass differences of tasks with every run counted on
    both sides; a task inconclusive under either harness is neither."""
    _check_same_tasks(current, candidate)
    diffs: tuple[int, ...] = ()
    if current_passes is not None and candidate_passes is not None:
        diffs = tuple(
            candidate_passes[t] - current_passes[t]
            for t in sorted(current)
            if t in current_passes and t in candidate_passes
        )
    wins = losses = compared = 0
    for task, before in current.items():
        after = candidate[task]
        if before is None or after is None:
            continue
        compared += 1
        if before is False and after is True:
            wins += 1
        elif before is True and after is False:
            losses += 1
    return HeldOutComparison(
        wins=wins, losses=losses, tasks=len(current), compared=compared, diffs=diffs
    )


def held_out_gate(comparison: HeldOutComparison, alpha: float = HELD_OUT_ALPHA) -> bool:
    """Pass when the candidate passes more held-out runs and a paired test on the per-task
    differences gives ``p < alpha`` (B12)."""
    return comparison.net_passes > 0 and comparison.p_value < alpha


@dataclass(frozen=True)
class Candidate:
    """A change that passed both gates; ``order`` is when its proposal was made."""

    proposal_id: str
    order: int
    held_out: HeldOutComparison
    context_tokens_added: int


def tie_break(candidates: Iterable[Candidate]) -> Candidate | None:
    """More held-out passes gained, then fewer added context tokens, then the earlier one."""
    return min(
        candidates,
        key=lambda c: (-c.held_out.net_passes, c.context_tokens_added, c.order),
        default=None,
    )


ABLATION_EVERY: Final = 3


def ablation_due(accepted_since_last: int, model_changed: bool) -> bool:
    """Ablate after every 3 accepted changes and after any change of the student model."""
    return model_changed or accepted_since_last >= ABLATION_EVERY


def ablation_prunes(
    train: HeldOutComparison, held_out: HeldOutComparison, alpha: float = ABLATION_ALPHA
) -> bool:
    """Drop a change unless the harness without it is clearly worse: a paired test on the
    pass differences over training plus held-out tasks (without minus current) gives
    ``p < alpha`` for the change helping. A change that cannot show it pays for its context.

    With no task measured under both harnesses anywhere, nothing is dropped: environment
    errors must not remove a change that may help.
    """
    diffs = train.diffs + held_out.diffs
    if not diffs:
        return False
    return paired_sign_flip_p([-d for d in diffs]) >= alpha
