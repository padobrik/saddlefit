"""The baseline runner runs control only when ``report --final`` asks for it (B10)."""

from dataclasses import fields

from saddlefit.loops.baseline import BASELINE_SPLITS, BaselineRunner


def test_the_baseline_runner_never_runs_control_by_default() -> None:
    default = next(f.default for f in fields(BaselineRunner) if f.name == "splits")
    assert default == BASELINE_SPLITS
    assert "control" not in default
