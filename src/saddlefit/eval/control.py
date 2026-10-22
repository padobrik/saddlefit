"""Control results as ``report --final`` shows them: aggregates only (B12, B15).

Per-task control outcomes go in; out come the success before and after with Wilson
intervals, a paired bootstrap interval for the difference, the delegation map per task type
and the context efficiency of the frozen harness.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from saddlefit.eval.statistics import context_efficiency, delegation_map, paired_bootstrap, wilson


def _rate(solved: int, n: int) -> dict[str, Any]:
    low, high = wilson(solved, n)
    return {"solved": solved, "wilson95": [round(low, 3), round(high, 3)]}


def control_summary(
    before: Mapping[str, bool | None],
    after: Mapping[str, bool | None],
    *,
    task_types: Mapping[str, str],
    mean_harness_tokens: float,
) -> dict[str, Any]:
    """Before and after over control tasks decided under both; the map uses ``after``."""
    tasks = sorted(before.keys() | after.keys())
    paired = [t for t in tasks if before.get(t) is not None and after.get(t) is not None]
    b = [bool(before[t]) for t in paired]
    a = [bool(after[t]) for t in paired]
    low, high = paired_bootstrap(b, a)
    by_type: dict[str, list[bool]] = defaultdict(list)
    for task in tasks:
        outcome = after.get(task)
        if outcome is not None:
            by_type[task_types.get(task, "unknown")].append(outcome)
    decided_after = [o for o in after.values() if o is not None]
    rate = sum(decided_after) / len(decided_after) if decided_after else 0.0
    return {
        "tasks": len(paired),
        "unmeasured": len(tasks) - len(paired),
        "before": _rate(sum(b), len(paired)),
        "after": _rate(sum(a), len(paired)),
        "difference95": [round(low, 3), round(high, 3)],
        "delegation": delegation_map(by_type),
        "context_efficiency": (
            round(context_efficiency(rate, mean_harness_tokens), 4)
            if mean_harness_tokens > 0
            else None
        ),
    }
