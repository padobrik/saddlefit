"""Required test 5 (gates, tie-break, ablation): table-driven tests of the B12 rules."""

import random

import pytest

from saddlefit.eval.decisions import (
    Candidate,
    HeldOutComparison,
    ablation_due,
    ablation_prunes,
    batch_gate,
    compare_held_out,
    held_out_gate,
    tie_break,
)


def outcomes(spec: str) -> dict[str, bool | None]:
    """'S' solved, 'u' unsolved, '?' inconclusive, one character per task t0, t1, …"""
    values = {"S": True, "u": False, "?": None}
    return {f"t{i}": values[c] for i, c in enumerate(spec)}


BATCH = [
    # (name, current, candidate, passes)
    ("one more solved", "Suuu", "SSuu", True),
    ("same count", "Suuu", "uSuu", False),
    ("same tasks solved", "SSuu", "SSuu", False),
    ("fewer solved", "SSuu", "Suuu", False),
    ("nothing solved either way", "uuuu", "uuuu", False),
    ("swap plus one", "SSuu", "uSSS", True),
    ("inconclusive is not solved", "uuu?", "uu?S", True),
    ("inconclusive does not help the candidate", "Suuu", "S???", False),
]


@pytest.mark.parametrize(("name", "current", "candidate", "passes"), BATCH)
def test_batch_gate(name: str, current: str, candidate: str, passes: bool) -> None:
    assert batch_gate(outcomes(current), outcomes(candidate)) is passes, name


COMPARISONS = [
    # (current, candidate, wins, losses)
    ("uuuuuu", "Suuuuu", 1, 0),
    ("SSuuuu", "uSSSuu", 2, 1),
    ("SSSSSS", "SSSSSS", 0, 0),
    ("u?Su", "?SSS", 1, 0),  # t0 and t1 are inconclusive on one side: neither win nor loss
    ("S?", "?u", 0, 0),
]


@pytest.mark.parametrize(("current", "candidate", "wins", "losses"), COMPARISONS)
def test_wins_and_losses(current: str, candidate: str, wins: int, losses: int) -> None:
    result = compare_held_out(outcomes(current), outcomes(candidate))
    assert (result.wins, result.losses, result.tasks) == (wins, losses, len(current))
    decided = sum(
        a is not None and b is not None
        for a, b in zip(outcomes(current).values(), outcomes(candidate).values(), strict=True)
    )
    assert result.compared == decided


def comparison(diffs: tuple[int, ...]) -> HeldOutComparison:
    return HeldOutComparison(wins=0, losses=0, tasks=len(diffs), diffs=diffs)


HELD_OUT = [
    # (name, per-task differences in passing runs, candidate minus current, passes)
    ("every task gains a run, six tasks", (1, 1, 1, 1, 1, 1), True),  # p = 1/64
    ("gains on three tasks only", (1, 1, 1, 0, 0, 0), False),  # p = 1/8: could be noise
    ("one big gain", (3, 0, 0, 0, 0, 0), False),  # p = 1/2
    ("gains offset by losses", (2, 2, -2, -2, 0, 0), False),
    ("no change", (0, 0, 0, 0, 0, 0), False),
    ("a net loss", (-1, -1, 0, 0, 0, 0), False),
    ("six clear gains with one loss", (2, 2, 2, 2, 2, -1), True),  # p = 2/64
    ("no held-out tasks", (), False),
]


@pytest.mark.parametrize(("name", "diffs", "passes"), HELD_OUT)
def test_held_out_gate(name: str, diffs: tuple[int, ...], passes: bool) -> None:
    assert held_out_gate(comparison(diffs)) is passes, name


def test_held_out_gate_alpha_is_a_parameter() -> None:
    six = comparison((2, 2, 2, 2, 2, -1))  # p = 2/64 ~ 0.031
    assert held_out_gate(six, alpha=0.05)
    assert not held_out_gate(six, alpha=0.05 / 3)  # three candidates share one alpha


def test_pass_differences_come_from_tasks_counted_on_both_sides() -> None:
    result = compare_held_out(
        outcomes("SuuS"), outcomes("SSuS"), {"t0": 3, "t1": 0, "t3": 2}, {"t0": 3, "t1": 2, "t2": 1}
    )
    assert result.diffs == (0, 2)  # t2 and t3 lack a count on one side
    assert result.net_passes == 2


def test_harnesses_must_be_run_on_the_same_tasks() -> None:
    with pytest.raises(ValueError, match="different tasks"):
        batch_gate(outcomes("SS"), outcomes("SSS"))
    with pytest.raises(ValueError, match="different tasks"):
        compare_held_out(outcomes("SS"), {"t0": True, "x": True})


def candidate(order: int, gained: int, tokens: int) -> Candidate:
    return Candidate(
        proposal_id=f"p{order}",
        order=order,
        held_out=comparison((gained,)),
        context_tokens_added=tokens,
    )


TIE_BREAKS = [
    # (name, candidates as (order, held-out passes gained, tokens), winner's order)
    ("more passes gained wins", [(0, 3, 10), (1, 5, 500)], 1),
    ("equal gain, fewer tokens", [(0, 4, 300), (1, 4, 40)], 1),
    ("equal gain and tokens, earlier proposal", [(1, 4, 40), (0, 4, 40)], 0),
    ("fewer tokens never beats a larger gain", [(0, 3, 0), (1, 4, 10_000)], 1),
    ("removed tokens count as fewer", [(0, 3, 40), (1, 3, -20)], 1),
    ("single candidate", [(4, 3, 5)], 4),
]


@pytest.mark.parametrize(("name", "specs", "winner"), TIE_BREAKS)
def test_tie_break(name: str, specs: list[tuple[int, int, int]], winner: int) -> None:
    chosen = tie_break(candidate(*spec) for spec in specs)
    assert chosen is not None
    assert chosen.order == winner, name


def test_tie_break_without_candidates() -> None:
    assert tie_break([]) is None


ABLATION_DUE = [
    # (accepted since the last ablation, student model changed, due)
    (0, False, False),
    (2, False, False),
    (3, False, True),
    (4, False, True),
    (0, True, True),
    (1, True, True),
]


@pytest.mark.parametrize(("accepted", "changed", "due"), ABLATION_DUE)
def test_ablation_due(accepted: int, changed: bool, due: bool) -> None:
    assert ablation_due(accepted, changed) is due


ABLATION_PRUNES = [
    # (name, train differences without the change, held-out differences, pruned)
    ("removing it changes nothing", (0, 0, 0), (0, 0, 0), True),
    ("removing it helps", (1, 1, 0), (1, 0, 0), True),
    ("removing it costs a little, maybe by chance", (-1, 0, 0), (-1, 0, 0), True),
    ("removing it costs clearly", (-2, -1, -2, -1), (-2, -1, -1, -2), False),  # p = 1/256
    ("losses offset by gains", (-2, -2, -2), (2, 2, 2), True),
]


def test_ablation_keeps_a_change_when_nothing_was_measured() -> None:
    nothing = compare_held_out(outcomes("??"), outcomes("S?"))
    assert (nothing.diffs, nothing.compared) == ((), 0)
    assert not ablation_prunes(nothing, nothing)
    # a measured held-out comparison alone is enough to decide
    assert ablation_prunes(nothing, comparison((0, 0, 0)))


@pytest.mark.parametrize(("name", "train", "held_out", "pruned"), ABLATION_PRUNES)
def test_ablation_prunes(
    name: str, train: tuple[int, ...], held_out: tuple[int, ...], pruned: bool
) -> None:
    assert ablation_prunes(comparison(train), comparison(held_out)) is pruned, name


# per-run pass rates like the v0.3.0 proof run's held-out tasks (about 70% on average)
RATES = (0.95, 0.9, 0.8, 0.7, 0.55, 0.3)


def simulated(rng: random.Random, tasks: int, runs: int, gain: float) -> HeldOutComparison:
    """Both harnesses on the same tasks: verdicts by majority, as the earlier rule used them."""
    current_passes, candidate_passes = {}, {}
    for i in range(tasks):
        p = RATES[i % len(RATES)]
        q = min(1.0, p + gain)
        current_passes[f"t{i}"] = sum(rng.random() < p for _ in range(runs))
        candidate_passes[f"t{i}"] = sum(rng.random() < q for _ in range(runs))
    majority = runs // 2 + 1
    return compare_held_out(
        {t: n >= majority for t, n in current_passes.items()},
        {t: n >= majority for t, n in candidate_passes.items()},
        current_passes,
        candidate_passes,
    )


def acceptance(tasks: int, runs: int, gain: float, trials: int = 600) -> float:
    rng = random.Random(7)
    return sum(held_out_gate(simulated(rng, tasks, runs, gain)) for _ in range(trials)) / trials


@pytest.mark.parametrize("tasks", [6, 12, 20])
def test_a_change_that_does_nothing_is_rarely_accepted(tasks: int) -> None:
    # the wins - losses rule accepted such a change 17-33% of the time at these sizes
    assert acceptance(tasks, runs=3, gain=0.0) < 0.05


def test_a_clear_gain_is_accepted_on_enough_tasks() -> None:
    assert acceptance(20, runs=5, gain=0.3) > 0.8
