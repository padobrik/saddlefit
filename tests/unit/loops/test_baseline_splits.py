"""The baseline runner runs control only when ``report --final`` asks for it (B10)."""

from dataclasses import fields
from pathlib import Path
from typing import Any

from saddlefit.config import Config
from saddlefit.harness.loop_config import default_loop_config
from saddlefit.harness.store import HarnessStore
from saddlefit.loops.baseline import BASELINE_SPLITS, BaselineRunner
from saddlefit.tasks.models import TaskMeta


def test_the_baseline_runner_never_runs_control_by_default() -> None:
    default = next(f.default for f in fields(BaselineRunner) if f.name == "splits")
    assert default == BASELINE_SPLITS
    assert "control" not in default


class Untouchable:
    """Fails if the runner reaches anything that would run a task."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"a control task reached the runner's {name}")


def test_run_skips_a_control_task(tmp_path: Path) -> None:
    store = HarnessStore(tmp_path / "harness")
    store.init()
    control = TaskMeta.model_validate(
        {
            "schema_version": 1,
            "id": "fix-date-parser-0042",
            "source": "history",
            "type": "bugfix",
            "split": "control",
            "repo": {"url": "https://github.com/OWNER/project", "base_commit": "3f2a9c1"},
            "created": "2026-10-21",
        }
    )
    runner = BaselineRunner(
        repo=tmp_path,
        config=Config(schema_version=2, project="p"),
        loop=default_loop_config(),
        harness_dir=store.path,
        harness_commit=store.head(),
        suite_hash="sha256:x",
        public_dir=tmp_path / "public",  # no task text exists: reading it would fail
        runs_root=tmp_path / "runs",
        client_factory=Untouchable,  # type: ignore[arg-type]
        evaluator=Untouchable(),  # type: ignore[arg-type]
        docker_client=Untouchable(),
        image="unused",
    )
    assert runner.run([control]).runs == []
