"""``fit``: middle-loop rounds that continue across invocations, with ablation (A7, B12)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from pathlib import Path

from saddlefit.eval.decisions import ablation_due
from saddlefit.loops.ablation import ablate
from saddlefit.loops.middle import MiddleLoop, RoundReport
from saddlefit.storage.fit_state import (
    FIT_STATE_SCHEMA_VERSION,
    FitState,
    load_fit_state,
    save_fit_state,
)


def run_rounds(
    loop: MiddleLoop,
    root: Path,
    rounds: int,
    on_round: Callable[[RoundReport], None] | None = None,
) -> list[RoundReport]:
    """Run ``rounds`` more rounds, continuing the state saved in ``root`` by the last run.

    Ablation runs before the first round when the student model changed since the last fit,
    and after any round that brings the accepted changes since the last ablation to three.
    """
    state = load_fit_state(root)
    accepted = state.accepted_since_ablation
    pending: list[str] = []
    model_changed = _model_changed(state, loop.student_model, loop.student_digest)
    # also resumes an ablation that a failure interrupted after its round was saved
    if ablation_due(accepted, model_changed=model_changed):
        pending = [r.proposal_id for r in ablate(loop) if r.pruned]
        accepted = 0
    done = state.rounds_done
    base = (state.tutor_calls - loop.spent_calls, state.tutor_usd - loop.spent_usd)

    def spent() -> tuple[int, float]:
        return (base[0] + loop.spent_calls, base[1] + loop.spent_usd)

    refusals = Counter(state.refusals)
    current = done

    def save(rounds_done: int) -> None:
        save_fit_state(root, _state(rounds_done, accepted, loop, spent(), refusals))

    # every tutor call is saved as soon as it is paid for: a crash loses at most the one in flight
    loop.on_charge = lambda: save(current)
    save(done)
    reports = []
    for index in range(done, done + rounds):
        current = index
        try:
            report = loop.round(index)
        except BaseException:  # Ctrl+C included
            # the round does not count, but what its tutor calls cost does
            save(index)
            raise
        report.pruned, pending = pending, []
        accepted += report.accepted is not None
        refusals.update(reason.split(":", 1)[0] for reason in report.refusals)
        # the round is saved before ablation, so a failing ablation never repeats it
        current = index + 1
        save(index + 1)
        if ablation_due(accepted, model_changed=False):
            report.pruned += [r.proposal_id for r in ablate(loop) if r.pruned]
            accepted = 0
            save(index + 1)
        reports.append(report)
        if on_round is not None:
            on_round(report)
    return reports


def _model_changed(state: FitState, model: str, digest: str | None) -> bool:
    """A new name, or the same name with new weights; unknown on a first fit."""
    if state.student_model is None:
        return False
    if state.student_model != model:
        return True
    return None not in {state.student_digest, digest} and state.student_digest != digest


def _state(
    rounds_done: int,
    accepted: int,
    loop: MiddleLoop,
    spent: tuple[int, float],
    refusals: Counter[str],
) -> FitState:
    return FitState(
        schema_version=FIT_STATE_SCHEMA_VERSION,
        rounds_done=rounds_done,
        accepted_since_ablation=accepted,
        student_model=loop.student_model,
        student_digest=loop.student_digest,
        tutor_calls=spent[0],
        tutor_usd=spent[1],
        refusals=dict(sorted(refusals.items())),
    )
