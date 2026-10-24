"""The baseline runner runs control only when ``report --final`` asks for it (B10)."""

from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

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


def control_task() -> TaskMeta:
    return TaskMeta.model_validate(
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


def runner(tmp_path: Path, **changes: Any) -> BaselineRunner:
    store = HarnessStore(tmp_path / "harness")
    store.init()
    fields_: dict[str, Any] = {
        "repo": tmp_path,
        "config": Config(schema_version=3, project="p"),
        "loop": default_loop_config(),
        "harness_dir": store.path,
        "harness_commit": store.head(),
        "suite_hash": "sha256:x",
        "public_dir": tmp_path / "public",  # no task text exists: reading it would fail
        "runs_root": tmp_path / "runs",
        "client_factory": Untouchable,
        "evaluator": Untouchable(),
        "docker_client": Untouchable(),
        "image": "unused",
    }
    return BaselineRunner(**{**fields_, **changes})


def test_run_skips_a_control_task(tmp_path: Path) -> None:
    assert runner(tmp_path).run([control_task()]).runs == []


def test_run_includes_control_when_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = control_task()
    (tmp_path / "public" / task.id).mkdir(parents=True)
    (tmp_path / "public" / task.id / "task.md").write_text("Fix it.", encoding="utf-8")
    monkeypatch.setattr("saddlefit.loops.baseline.export_tree", lambda repo, commit, dest: None)
    asked: list[str] = []
    final = runner(tmp_path, splits=frozenset({"control"}))

    def run_once(**kwargs: Any) -> Any:
        asked.append(kwargs["meta"].split)
        raise StopIteration  # stand-in for a real run; nothing else is needed

    monkeypatch.setattr(final, "_run_with_retry", run_once)
    with pytest.raises(StopIteration):
        final.run([task])
    assert asked == ["control"]
