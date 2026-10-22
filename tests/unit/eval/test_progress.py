"""Before and after from run records: grouped by harness tree, aggregates only (B15)."""

from pathlib import Path
from typing import Any

import pytest

from saddlefit.eval.progress import Comparable, harness_progress
from saddlefit.storage.runs import RunSummary, RunWriter, new_run_id

# a candidate commit and the merge that accepted it share one tree
TREES = {"aaaaaaa": "tree-before", "bbbbbbb": "tree-after", "ccccccc": "tree-after"}
TYPES = {"t1": "bugfix", "t2": "bugfix", "t3": "refactor", "h1": "bugfix", "h2": "bugfix"}


TASKS = {"train": ["t1", "t2", "t3"], "held_out": ["h1", "h2"]}
SAME = Comparable(model="student", digest="sha256:m", suite_hash="sha256:x")


def run(
    root: Path,
    commit: str,
    task: str,
    split: str,
    passed: bool,
    *,
    tag: str | None = None,
    model: str = "student",
    digest: str | None = "sha256:m",
    suite: str = "sha256:x",
) -> None:
    run_id = new_run_id()
    summary: dict[str, Any] = {
        "schema_version": 1,
        "type": "summary",
        "run_id": run_id,
        "task_id": task,
        "split": split,
        "attempt": 1,
        "model": {"id": model, "digest": digest},
        "harness_commit": commit,
        "saddlefit_version": "0.2.0",
        "suite_hash": suite,
        "sandbox_image": "sha256:y",
        "seed": 7,
        "steps": 1,
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "harness_tokens": 1,
        "wall_seconds": 1.0,
        "outcome": "pass" if passed else "fail",
        "primary_tag": tag or ("solved" if passed else "gave_up"),
        "tags": [tag or ("solved" if passed else "gave_up")],
    }
    RunWriter(root, run_id).finish(RunSummary.model_validate(summary))


def progress(root: Path, runs_per_task: int = 1, threshold: int = 1) -> dict[str, Any]:
    return harness_progress(
        root,
        tree_of=TREES.get,
        before_tree="tree-before",
        after_tree="tree-after",
        tasks=TASKS,
        task_types=TYPES,
        comparable=SAME,
        runs_per_task=runs_per_task,
        threshold=threshold,
    )


def test_before_and_after_over_tasks_decided_under_both(tmp_path: Path) -> None:
    run(tmp_path, "aaaaaaa", "t1", "train", False)
    run(tmp_path, "aaaaaaa", "t2", "train", True)
    run(tmp_path, "aaaaaaa", "t3", "train", False)
    run(tmp_path, "bbbbbbb", "t1", "train", True)  # the candidate's run
    run(tmp_path, "ccccccc", "t2", "train", True)  # the merge's run, same tree
    run(tmp_path, "aaaaaaa", "h1", "held_out", False)
    run(tmp_path, "bbbbbbb", "h1", "held_out", True)
    run(tmp_path, "bbbbbbb", "h2", "held_out", False)  # never run before: not paired
    result = progress(tmp_path)

    train = result["splits"]["train"]
    assert train["tasks"] == 2  # t3 was only run before
    assert train["unmeasured"] == 1
    assert (train["before"]["solved"], train["after"]["solved"]) == (1, 2)
    assert train["by_type"] == {"bugfix": {"tasks": 2, "before": 1, "after": 2}}
    held_out = result["splits"]["held_out"]
    assert (held_out["tasks"], held_out["unmeasured"]) == (1, 1)
    assert (held_out["before"]["solved"], held_out["after"]["solved"]) == (0, 1)
    assert held_out["difference95"] == [1.0, 1.0]
    # the failure profile counts the current harness's runs only
    assert result["failure_profile"] == {"solved": 3, "gave_up": 1}


def test_control_runs_and_other_harnesses_are_left_out(tmp_path: Path) -> None:
    run(tmp_path, "aaaaaaa", "c1", "control", True)
    run(tmp_path, "bbbbbbb", "c1", "control", True)
    run(tmp_path, "ddddddd", "t1", "train", True)  # a harness that is neither
    result = progress(tmp_path)
    assert all(row["tasks"] == 0 for row in result["splits"].values())
    # every suite task counts as not measured, run or not
    assert [row["unmeasured"] for row in result["splits"].values()] == [3, 2]
    assert result["failure_profile"] == {}


def test_runs_cut_off_by_a_crash_are_left_out(tmp_path: Path) -> None:
    run(tmp_path, "aaaaaaa", "t1", "train", True)
    run(tmp_path, "bbbbbbb", "t1", "train", True)
    interrupted = RunWriter(tmp_path, new_run_id())
    interrupted.event("error", {"kind": "x", "message": "y"})  # no summary follows
    result = progress(tmp_path)
    assert result["splits"]["train"]["tasks"] == 1


def test_environment_errors_leave_a_task_undecided(tmp_path: Path) -> None:
    run(tmp_path, "aaaaaaa", "t1", "train", False)
    run(tmp_path, "bbbbbbb", "t1", "train", False, tag="env_error")
    result = progress(tmp_path)
    assert result["splits"]["train"]["tasks"] == 0
    assert result["splits"]["train"]["unmeasured"] == 3


def test_a_task_is_judged_on_its_latest_evaluation(tmp_path: Path) -> None:
    # an earlier evaluation passed twice, a later one failed all three runs
    for passed in (True, True, False, False, False, False):
        run(tmp_path, "bbbbbbb", "t1", "train", passed)
    for passed in (False, False, False):
        run(tmp_path, "aaaaaaa", "t1", "train", passed)
    train = progress(tmp_path, runs_per_task=3, threshold=2)["splits"]["train"]
    assert train["tasks"] == 1
    assert train["after"]["solved"] == 0


@pytest.mark.parametrize(
    "other",
    [{"model": "llama3:8b"}, {"digest": "sha256:other"}, {"suite": "sha256:grown"}],
)
def test_runs_from_another_model_or_suite_are_not_compared(
    tmp_path: Path, other: dict[str, Any]
) -> None:
    run(tmp_path, "aaaaaaa", "t1", "train", False)
    run(tmp_path, "bbbbbbb", "t1", "train", True, **other)
    assert progress(tmp_path)["splits"]["train"]["tasks"] == 0


def test_a_missing_digest_does_not_exclude_runs(tmp_path: Path) -> None:
    run(tmp_path, "aaaaaaa", "t1", "train", False)
    run(tmp_path, "bbbbbbb", "t1", "train", True, digest=None)
    assert progress(tmp_path)["splits"]["train"]["tasks"] == 1
