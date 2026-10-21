import json
from pathlib import Path

import pytest

from saddlefit.eval.taxonomy import Classification
from saddlefit.loops.baseline import BaselineResult, RunOutcome
from saddlefit.reports.baseline import (
    REPORT_SCHEMA_VERSION,
    ReportContext,
    build,
    render_markdown,
    wilson,
    write,
)

CTX = ReportContext(
    model="qwen3:8b",
    model_digest="sha256:abc",
    harness_commit="a" * 40,
    suite_hash="sha256:def",
    saddlefit_version="0.1.0",
)


def run(task: str, split: str, passed: bool, tag: str) -> RunOutcome:
    return RunOutcome(
        task_id=task,
        split=split,
        run_id=f"{task}-{passed}",
        seed=7,
        passed=passed,
        classification=Classification(primary=tag, tags=(tag,)),  # type: ignore[arg-type]
        harness_tokens=300,
    )


@pytest.mark.parametrize(
    ("k", "n", "low", "high"),
    [(0, 10, 0.0, 0.2775), (5, 10, 0.2366, 0.7634), (10, 10, 0.7225, 1.0), (1, 1, 0.2065, 1.0)],
)
def test_wilson_matches_known_values(k: int, n: int, low: float, high: float) -> None:
    lo, hi = wilson(k, n)
    assert lo == pytest.approx(low, abs=1e-3)
    assert hi == pytest.approx(high, abs=1e-3)


def test_wilson_without_data() -> None:
    assert wilson(0, 0) == (0.0, 1.0)


def test_report_aggregates_and_hides_held_out_detail(tmp_path: Path) -> None:
    result = BaselineResult(
        runs=[
            run("t1", "train", True, "solved"),
            run("t1", "train", True, "solved"),
            run("t2", "train", False, "gave_up"),
            run("t2", "train", True, "solved"),
            run("h1", "held_out", False, "loop"),
            run("h1", "held_out", False, "loop"),
        ],
        runs_per_task=2,
        solved_threshold=2,
    )
    report = build(result, CTX)
    assert report["splits"]["train"]["solved"] == 1
    assert report["splits"]["held_out"] == {"solved": 0, "tasks": 1, "wilson95": [0.0, 0.793]}
    assert report["train_tasks"] == {"t1": True, "t2": False}
    assert "h1" not in json.dumps(report)  # held-out outcomes only as aggregates
    assert report["failure_profile"] == {"solved": 3, "loop": 2, "gave_up": 1}
    assert report["control_evaluations"] == 0
    md, js = write(report, tmp_path, "baseline-1")
    assert "| train | 1 | 2 | 50% |" in md.read_text(encoding="utf-8")
    assert json.loads(js.read_text(encoding="utf-8"))["kind"] == "baseline"
    assert "qwen3:8b" in render_markdown(report)


def test_environment_errors_stay_out_of_the_rates() -> None:
    result = BaselineResult(runs_per_task=1, solved_threshold=1)
    result.runs += [
        run("a", "train", True, "solved"),
        run("b", "train", False, "gave_up"),
        run("c", "train", False, "env_error"),  # never a model attempt
    ]
    assert result.solved() == {"a": True, "b": False}
    assert result.inconclusive() == ["c"]
    assert result.by_split() == {"train": (1, 2)}
    report = build(result, CTX)
    assert report["excluded_runs"] == 1
    assert report["inconclusive_tasks"] == 1
    assert "excluded" in render_markdown(report)


def test_both_report_files_carry_the_schema_version(tmp_path: Path) -> None:
    result = BaselineResult(runs_per_task=1, solved_threshold=1)
    result.runs.append(run("a", "train", True, "solved"))
    markdown_path, json_path = write(build(result, CTX), tmp_path, "baseline-x")
    assert (
        json.loads(json_path.read_text(encoding="utf-8"))["schema_version"] == REPORT_SCHEMA_VERSION
    )
    assert f"schema_version: {REPORT_SCHEMA_VERSION}" in markdown_path.read_text(encoding="utf-8")
