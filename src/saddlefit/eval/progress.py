"""Before and after: how the student did with two harnesses, as aggregates only (B15).

The evaluator reads every training and held-out run record, groups the runs by the git tree
of the harness they used (a candidate and the merge that accepted it share a tree), decides
each task under each harness as B12 does, and compares the tasks decided under both. Only
counts, intervals and per-type counts leave this module; control runs are never read here.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from saddlefit.eval.records import read_any_run
from saddlefit.eval.statistics import paired_bootstrap, wilson
from saddlefit.storage.runs import RunSummary, UnfinishedRun, find_runs

SPLITS = ("train", "held_out")


def _decide(runs: Sequence[RunSummary], runs_per_task: int, threshold: int) -> bool | None:
    """One task under one harness, as ``BaselineResult.solved`` decides it; None if
    inconclusive. Environment errors are no attempt by the model (B9)."""
    counted = [r for r in runs if r.primary_tag != "env_error"]
    passes = sum(r.outcome == "pass" for r in counted)
    missing = max(runs_per_task - len(counted), 0)
    if passes >= threshold:
        return True
    if passes + missing < threshold:
        return False
    return None


def _rate(solved: int, n: int) -> dict[str, Any]:
    low, high = wilson(solved, n)
    return {"solved": solved, "wilson95": [round(low, 3), round(high, 3)]}


@dataclass(frozen=True)
class Comparable:
    """Runs count only if they used the baseline's student model and task suite."""

    model: str
    digest: str | None
    suite_hash: str

    def admits(self, summary: RunSummary) -> bool:
        if summary.model.id != self.model or summary.suite_hash != self.suite_hash:
            return False
        return None in {self.digest, summary.model.digest} or summary.model.digest == self.digest


def harness_progress(
    runs_root: Path,
    *,
    tree_of: Callable[[str], str | None],
    before_tree: str,
    after_tree: str,
    tasks: Mapping[str, Sequence[str]],
    task_types: Mapping[str, str],
    comparable: Comparable,
    runs_per_task: int,
    threshold: int,
) -> dict[str, Any]:
    """Training and held-out success before and after, over tasks decided under both.

    ``tasks`` holds the suite's task IDs per split: every one that is not decided under both
    harnesses counts as not measured. A task evaluated more than once under a harness is
    judged on its latest ``runs_per_task`` runs, one evaluation's worth.
    """
    runs: dict[tuple[str, str, str], list[RunSummary]] = defaultdict(list)
    trees: dict[str, str | None] = {}
    for path in find_runs(runs_root):  # oldest first: ULIDs sort by time
        try:
            _, summary = read_any_run(path)
        except UnfinishedRun:
            continue  # a run cut off by a crash has no outcome to compare
        if summary.split not in SPLITS or not comparable.admits(summary):
            continue  # control runs are reported only by `report --final`
        commit = summary.harness_commit
        if commit not in trees:
            trees[commit] = tree_of(commit)
        tree = trees[commit]
        if tree in {before_tree, after_tree}:
            runs[(tree, summary.split, summary.task_id)].append(summary)
    latest = {key: group[-runs_per_task:] for key, group in runs.items()}
    profile: Counter[str] = Counter(
        r.primary_tag for (tree, _, _), group in latest.items() if tree == after_tree for r in group
    )
    report: dict[str, Any] = {"splits": {}, "failure_profile": dict(profile.most_common())}
    for split in SPLITS:
        ids = sorted(tasks.get(split, ()))
        decided = {
            t: (
                _decide(latest.get((before_tree, split, t), []), runs_per_task, threshold),
                _decide(latest.get((after_tree, split, t), []), runs_per_task, threshold),
            )
            for t in ids
        }
        paired = [t for t in ids if None not in decided[t]]
        b = [bool(decided[t][0]) for t in paired]
        a = [bool(decided[t][1]) for t in paired]
        low, high = paired_bootstrap(b, a)
        by_type: dict[str, dict[str, int]] = defaultdict(
            lambda: {"tasks": 0, "before": 0, "after": 0}
        )
        for task, before, after in zip(paired, b, a, strict=True):
            row = by_type[task_types.get(task, "unknown")]
            row["tasks"] += 1
            row["before"] += before
            row["after"] += after
        report["splits"][split] = {
            "tasks": len(paired),
            "unmeasured": len(ids) - len(paired),
            "before": _rate(sum(b), len(paired)),
            "after": _rate(sum(a), len(paired)),
            "difference95": [round(low, 3), round(high, 3)],
            "by_type": dict(sorted(by_type.items())),
        }
    return report


def task_types_of(metas: Sequence[Any]) -> dict[str, str]:
    """Task ID to type, from task metadata."""
    return {m.id: m.type for m in metas}
