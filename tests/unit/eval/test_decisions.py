"""Required test 5 (gates and tie-break): table-driven tests of the B12 decision rules."""

import pytest

from saddlefit.eval.decisions import (
    Candidate,
    HeldOutComparison,
    batch_gate,
    compare_held_out,
    held_out_gate,
    required_net,
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


REQUIRED_NET = [
    # (tasks, margin, net)
    (0, 0.1, 1),
    (1, 0.1, 1),
    (6, 0.1, 1),
    (10, 0.1, 1),
    (11, 0.1, 2),
    (20, 0.1, 2),
    (30, 0.1, 3),
    (71, 0.1, 8),
    (100, 0.07, 7),  # 0.07 * 100 is a hair above 7 in floats
    (101, 0.07, 8),
]


@pytest.mark.parametrize(("tasks", "margin", "net"), REQUIRED_NET)
def test_required_net_is_exact(tasks: int, margin: float, net: int) -> None:
    assert required_net(tasks, margin) == net


HELD_OUT = [
    # (name, tasks, wins, losses, passes)
    ("one win of six", 6, 1, 0, True),
    ("no change", 6, 0, 0, False),
    ("net one with one loss", 6, 2, 1, True),
    ("net one with two losses", 6, 3, 2, False),
    ("net zero", 6, 1, 1, False),
    ("ten tasks need one", 10, 1, 0, True),
    ("eleven tasks need two", 11, 1, 0, False),
    ("eleven tasks, two wins", 11, 2, 0, True),
    ("thirty tasks need three", 30, 3, 0, True),
    ("thirty tasks, net three with one loss", 30, 4, 1, True),
    ("thirty tasks, net two", 30, 3, 1, False),
    ("no held-out tasks", 0, 0, 0, False),
]


@pytest.mark.parametrize(("name", "tasks", "wins", "losses", "passes"), HELD_OUT)
def test_held_out_gate(name: str, tasks: int, wins: int, losses: int, passes: bool) -> None:
    comparison = HeldOutComparison(wins=wins, losses=losses, tasks=tasks)
    assert held_out_gate(comparison) is passes, name


def test_held_out_gate_settings_are_parameters() -> None:
    comparison = HeldOutComparison(wins=3, losses=2, tasks=10)
    assert not held_out_gate(comparison)
    assert held_out_gate(comparison, max_losses=2)
    assert not held_out_gate(HeldOutComparison(wins=2, losses=0, tasks=10), margin=0.25)


def test_harnesses_must_be_run_on_the_same_tasks() -> None:
    with pytest.raises(ValueError, match="different tasks"):
        batch_gate(outcomes("SS"), outcomes("SSS"))
    with pytest.raises(ValueError, match="different tasks"):
        compare_held_out(outcomes("SS"), {"t0": True, "x": True})


def candidate(order: int, wins: int, losses: int, tokens: int) -> Candidate:
    return Candidate(
        proposal_id=f"p{order}",
        order=order,
        held_out=HeldOutComparison(wins=wins, losses=losses, tasks=6),
        context_tokens_added=tokens,
    )


TIE_BREAKS = [
    # (name, candidates as (order, wins, losses, tokens), winner's order)
    ("larger net wins", [(0, 1, 0, 10), (1, 2, 0, 500)], 1),
    ("net, not wins", [(0, 3, 2, 10), (1, 2, 0, 500)], 1),
    ("equal net, fewer tokens", [(0, 2, 0, 300), (1, 2, 0, 40)], 1),
    ("equal net and tokens, earlier proposal", [(1, 2, 0, 40), (0, 2, 0, 40)], 0),
    ("fewer tokens never beats a larger net", [(0, 1, 0, 0), (1, 2, 0, 10_000)], 1),
    ("removed tokens count as fewer", [(0, 1, 0, 40), (1, 1, 0, -20)], 1),
    ("single candidate", [(4, 1, 0, 5)], 4),
]


@pytest.mark.parametrize(("name", "specs", "winner"), TIE_BREAKS)
def test_tie_break(name: str, specs: list[tuple[int, int, int, int]], winner: int) -> None:
    chosen = tie_break(candidate(*spec) for spec in specs)
    assert chosen is not None
    assert chosen.order == winner, name


def test_tie_break_without_candidates() -> None:
    assert tie_break([]) is None
