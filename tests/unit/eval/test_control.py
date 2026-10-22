"""Control aggregates for ``report --final`` (B12, B15)."""

import pytest

from saddlefit.eval.control import control_summary

TYPES = {f"b{i}": "bugfix" for i in range(6)} | {"r0": "refactor", "r1": "refactor"}


def test_control_before_and_after() -> None:
    before = dict.fromkeys(TYPES, False) | {"b0": True, "r1": None}
    after = {f"b{i}": True for i in range(6)} | {"r0": False, "r1": True}
    summary = control_summary(before, after, task_types=TYPES, mean_harness_tokens=2000)
    assert summary["tasks"] == 7  # r1 was inconclusive before
    assert summary["unmeasured"] == 1
    assert (summary["before"]["solved"], summary["after"]["solved"]) == (1, 6)
    assert summary["difference95"][0] > 0
    # the delegation map uses the frozen harness's outcomes, all decided ones
    assert summary["delegation"] == {"bugfix": "local", "refactor": "insufficient data"}
    # 7 of 8 decided after, per 1,000 harness tokens
    assert summary["context_efficiency"] == pytest.approx(round(7 / 8 / 2, 4))


def test_no_control_tasks() -> None:
    summary = control_summary({}, {}, task_types={}, mean_harness_tokens=0)
    assert (summary["tasks"], summary["unmeasured"]) == (0, 0)
    assert summary["delegation"] == {}
    assert summary["context_efficiency"] is None
