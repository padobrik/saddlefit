"""Required test 5 (delegation map) and the report statistics (B12, B15)."""

import pytest

from saddlefit.eval.statistics import (
    context_efficiency,
    delegation,
    delegation_map,
    paired_bootstrap,
    wilson,
)


def runs(solved: int, n: int) -> list[bool]:
    return [True] * solved + [False] * (n - solved)


DELEGATION = [
    # (name, solved, control tasks, verdict)
    ("all solved", 5, 5, "local"),  # 100%, Wilson low 0.566
    ("90 percent", 9, 10, "local"),  # Wilson low 0.596
    ("80 percent of 20", 16, 20, "local"),  # Wilson low 0.584
    ("80 percent of 5: too few for local", 4, 5, "borderline"),  # Wilson low 0.376
    ("80 percent of 10: Wilson low just under 0.5", 8, 10, "borderline"),  # 0.490
    ("60 percent", 3, 5, "borderline"),
    ("exactly half", 5, 10, "borderline"),
    ("40 percent", 4, 10, "cloud"),
    ("none solved", 0, 6, "cloud"),
    ("four control tasks", 4, 4, "insufficient data"),
    ("no control tasks", 0, 0, "insufficient data"),
]


@pytest.mark.parametrize(("name", "solved", "n", "verdict"), DELEGATION)
def test_delegation(name: str, solved: int, n: int, verdict: str) -> None:
    assert delegation(runs(solved, n)) == verdict, name


def test_delegation_map_by_task_type() -> None:
    assert delegation_map({"refactor": runs(1, 6), "bugfix": runs(9, 10), "docs": runs(2, 2)}) == {
        "bugfix": "local",
        "docs": "insufficient data",
        "refactor": "cloud",
    }


@pytest.mark.parametrize(
    ("k", "n", "low", "high"), [(0, 10, 0.0, 0.278), (5, 10, 0.237, 0.763), (10, 10, 0.722, 1.0)]
)
def test_wilson_matches_known_values(k: int, n: int, low: float, high: float) -> None:
    lo, hi = wilson(k, n)
    assert (round(lo, 3), round(hi, 3)) == (low, high)


def test_bootstrap_bounds_for_clear_cases() -> None:
    assert paired_bootstrap([False] * 8, [True] * 8) == (1.0, 1.0)
    # identical outcomes give exactly zero only if each task keeps its own pair
    assert paired_bootstrap([True, False] * 4, [True, False] * 4) == (0.0, 0.0)
    assert paired_bootstrap([], []) == (0.0, 0.0)


def test_bootstrap_is_reproducible_and_brackets_the_observed_difference() -> None:
    before = [False, False, True, False, True, False, False, True, False, False]
    after = [True, False, True, True, True, False, True, True, False, True]  # +4 of 10
    low, high = paired_bootstrap(before, after)
    assert (low, high) == paired_bootstrap(before, after)  # fixed seed
    assert low <= 0.4 <= high
    assert 0.0 < low < high <= 0.8


def test_bootstrap_needs_the_same_tasks() -> None:
    with pytest.raises(ValueError, match="same tasks"):
        paired_bootstrap([True], [True, False])


def test_context_efficiency() -> None:
    assert context_efficiency(0.6, 3000) == pytest.approx(0.2)
    with pytest.raises(ValueError, match="positive"):
        context_efficiency(0.6, 0)
