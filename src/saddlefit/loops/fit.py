"""``fit``: middle-loop rounds that continue across invocations, with ablation (A7, B12)."""

from __future__ import annotations

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
    model_changed = state.student_model not in {None, loop.student_model}
    # also resumes an ablation that a failure interrupted after its round was saved
    if ablation_due(accepted, model_changed=model_changed):
        pending = [r.proposal_id for r in ablate(loop) if r.pruned]
        accepted = 0
    done = state.rounds_done
    save_fit_state(root, _state(done, accepted, loop.student_model))
    reports = []
    for index in range(done, done + rounds):
        report = loop.round(index)
        report.pruned, pending = pending, []
        accepted += report.accepted is not None
        # the round is saved before ablation, so a failing ablation never repeats it
        save_fit_state(root, _state(index + 1, accepted, loop.student_model))
        if ablation_due(accepted, model_changed=False):
            report.pruned += [r.proposal_id for r in ablate(loop) if r.pruned]
            accepted = 0
            save_fit_state(root, _state(index + 1, accepted, loop.student_model))
        reports.append(report)
        if on_round is not None:
            on_round(report)
    return reports


def _state(rounds_done: int, accepted: int, student_model: str) -> FitState:
    return FitState(
        schema_version=FIT_STATE_SCHEMA_VERSION,
        rounds_done=rounds_done,
        accepted_since_ablation=accepted,
        student_model=student_model,
    )
