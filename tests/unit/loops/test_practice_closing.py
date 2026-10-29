"""The closing evaluation must decide every run, or fit says so (#163)."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from saddlefit.config import Config
from saddlefit.errors import EnvironmentFailure
from saddlefit.loops.baseline import BaselineResult, RunOutcome
from saddlefit.loops.practice import DockerPractice


def outcome(task_id: str, split: str, counts: bool) -> RunOutcome:
    return RunOutcome(
        task_id=task_id,
        split=split,
        run_id=f"run-{task_id}",
        seed=7,
        passed=counts,
        classification=SimpleNamespace(counts_in_statistics=counts),  # type: ignore[arg-type]
        harness_tokens=0,
    )


def practice(runs: list[RunOutcome], monkeypatch: pytest.MonkeyPatch) -> DockerPractice:
    metas = [SimpleNamespace(id=r.task_id, split=r.split) for r in runs]
    made = DockerPractice(
        repo=Path("."),
        config=Config(schema_version=3, project="p"),
        store=None,  # type: ignore[arg-type]
        tasks=metas,  # type: ignore[arg-type]
        public_dir=Path("."),
        runs_root=Path("."),
        suite_hash="sha256:x",
        client_factory=lambda: None,  # type: ignore[arg-type,return-value]
        evaluator=None,  # type: ignore[arg-type]
        docker_client=None,
        image="img",
    )
    monkeypatch.setattr(made, "_run", lambda ref, chosen: BaselineResult(runs=runs))
    return made


def test_a_decided_closing_evaluation_returns_its_run_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = [outcome("t1", "train", True), outcome("h1", "held_out", True)]
    assert practice(runs, monkeypatch).evaluate("main") == 2


def test_an_undecided_run_fails_the_closing_evaluation_without_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: list[Any] = [outcome("t1", "train", True), outcome("held-secret", "held_out", False)]
    with pytest.raises(EnvironmentFailure, match="could not decide 1 of 2 runs") as raised:
        practice(runs, monkeypatch).evaluate("main")
    assert "held-secret" not in str(raised.value)  # a held-out task is never named (B10)
