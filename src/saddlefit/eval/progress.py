"""Before and after: how the student did with two harnesses, as aggregates only (B15).

The evaluator reads every training and held-out run record, groups the runs by the git tree
of the harness they used (a candidate and the merge that accepted it share a tree), decides
each task under each harness as B12 does, and compares the tasks decided under both. Only
counts, intervals and per-type counts leave this module; control runs are never read here.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from saddlefit.eval.records import read_any_run
from saddlefit.eval.statistics import paired_bootstrap, wilson
from saddlefit.storage.runs import RunSummary, find_runs

SPLITS = ("train", "held_out")


@dataclass
class _Runs:
    passes: Counter[str] = field(default_factory=Counter)
    counted: Counter[str] = field(default_factory=Counter)


def _decide(runs: _Runs, runs_per_task: int, threshold: int) -> dict[str, bool]:
    """Decided tasks only, as ``BaselineResult.solved`` decides them."""
    decided = {}
    for task, counted in runs.counted.items():
        missing = max(runs_per_task - counted, 0)
        if runs.passes[task] >= threshold:
            decided[task] = True
        elif runs.passes[task] + missing < threshold:
            decided[task] = False
    return decided


def _rate(solved: int, n: int) -> dict[str, Any]:
    low, high = wilson(solved, n)
    return {"solved": solved, "wilson95": [round(low, 3), round(high, 3)]}


def harness_progress(
    runs_root: Path,
    *,
    tree_of: Callable[[str], str | None],
    before_tree: str,
    after_tree: str,
    task_types: Mapping[str, str],
    runs_per_task: int,
    threshold: int,
) -> dict[str, Any]:
    """Training and held-out success before and after, over tasks decided under both."""
    groups: dict[tuple[str, str], _Runs] = defaultdict(_Runs)
    profile: Counter[str] = Counter()
    trees: dict[str, str | None] = {}
    for path in find_runs(runs_root):
        _, summary = read_any_run(path)
        if summary.split not in SPLITS:
            continue  # control runs are reported only by `report --final`
        commit = summary.harness_commit
        if commit not in trees:
            trees[commit] = tree_of(commit)
        tree = trees[commit]
        if tree not in {before_tree, after_tree}:
            continue
        _count(groups[(tree, summary.split)], summary)
        if tree == after_tree:
            profile[summary.primary_tag] += 1
    report: dict[str, Any] = {"splits": {}, "failure_profile": dict(profile.most_common())}
    for split in SPLITS:
        before = _decide(groups[(before_tree, split)], runs_per_task, threshold)
        after = _decide(groups[(after_tree, split)], runs_per_task, threshold)
        paired = sorted(before.keys() & after.keys())
        b = [before[t] for t in paired]
        a = [after[t] for t in paired]
        low, high = paired_bootstrap(b, a)
        by_type: dict[str, dict[str, int]] = defaultdict(
            lambda: {"tasks": 0, "before": 0, "after": 0}
        )
        for task in paired:
            row = by_type[task_types.get(task, "unknown")]
            row["tasks"] += 1
            row["before"] += before[task]
            row["after"] += after[task]
        report["splits"][split] = {
            "tasks": len(paired),
            "unmeasured": len(before.keys() ^ after.keys()),
            "before": _rate(sum(b), len(paired)),
            "after": _rate(sum(a), len(paired)),
            "difference95": [round(low, 3), round(high, 3)],
            "by_type": dict(sorted(by_type.items())),
        }
    return report


def _count(runs: _Runs, summary: RunSummary) -> None:
    if summary.primary_tag == "env_error":
        return  # no attempt by the model (B9); the task may stay inconclusive
    runs.counted[summary.task_id] += 1
    runs.passes[summary.task_id] += summary.outcome == "pass"


def task_types_of(metas: Sequence[Any]) -> dict[str, str]:
    """Task ID to type, from task metadata."""
    return {m.id: m.type for m in metas}
