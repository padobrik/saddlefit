"""Before and after from run records: grouped by harness tree, aggregates only (B15)."""

from pathlib import Path
from typing import Any

from saddlefit.eval.progress import harness_progress
from saddlefit.storage.runs import RunSummary, RunWriter, new_run_id

# a candidate commit and the merge that accepted it share one tree
TREES = {"aaaaaaa": "tree-before", "bbbbbbb": "tree-after", "ccccccc": "tree-after"}
TYPES = {"t1": "bugfix", "t2": "bugfix", "t3": "refactor", "h1": "bugfix", "h2": "bugfix"}


def run(
    root: Path, commit: str, task: str, split: str, passed: bool, *, tag: str | None = None
) -> None:
    run_id = new_run_id()
    summary: dict[str, Any] = {
        "schema_version": 1,
        "type": "summary",
        "run_id": run_id,
        "task_id": task,
        "split": split,
        "attempt": 1,
        "model": {"id": "student"},
        "harness_commit": commit,
        "saddlefit_version": "0.2.0",
        "suite_hash": "sha256:x",
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


def progress(root: Path) -> dict[str, Any]:
    return harness_progress(
        root,
        tree_of=TREES.get,
        before_tree="tree-before",
        after_tree="tree-after",
        task_types=TYPES,
        runs_per_task=1,
        threshold=1,
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
    assert result["failure_profile"] == {}


def test_environment_errors_leave_a_task_undecided(tmp_path: Path) -> None:
    run(tmp_path, "aaaaaaa", "t1", "train", False)
    run(tmp_path, "bbbbbbb", "t1", "train", False, tag="env_error")
    result = progress(tmp_path)
    assert result["splits"]["train"]["tasks"] == 0
    assert result["splits"]["train"]["unmeasured"] == 1
