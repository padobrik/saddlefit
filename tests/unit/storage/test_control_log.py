from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from saddlefit.errors import EnvironmentFailure
from saddlefit.schema_registry import SCHEMA_MODELS
from saddlefit.storage.control_log import (
    ControlEvaluation,
    ControlLog,
    control_evaluations,
    load_control_log,
    log_control_evaluation,
)


def evaluation(**changes: Any) -> ControlEvaluation:
    fields: dict[str, Any] = {
        "tag": "fit-2026-10-22-1",
        "harness_commit": "b4c5d6e",
        "baseline_harness_commit": "a1b2c3d",
        "created": "2026-10-22T10:00:00+00:00",
        "tasks": 6,
    }
    return ControlEvaluation.model_validate({**fields, **changes})


def test_no_control_evaluation_yet(tmp_path: Path) -> None:
    assert control_evaluations(tmp_path) == 0


def test_every_evaluation_is_logged_and_counted(tmp_path: Path) -> None:
    log_control_evaluation(tmp_path, evaluation())
    log_control_evaluation(tmp_path, evaluation(tag="fit-2026-10-22-2"))
    assert control_evaluations(tmp_path) == 2
    assert [e.tag for e in load_control_log(tmp_path).evaluations] == [
        "fit-2026-10-22-1",
        "fit-2026-10-22-2",
    ]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["control.json"]  # lock released


def test_parallel_appends_lose_nothing(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: log_control_evaluation(tmp_path, evaluation(tasks=i)), range(16)))
    assert sorted(e.tasks for e in load_control_log(tmp_path).evaluations) == list(range(16))


def test_a_stuck_lock_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("saddlefit.storage.control_log.LOCK_WAIT_S", 0.1)
    (tmp_path / "control.lock").write_text("")
    with pytest.raises(EnvironmentFailure, match="control log is locked"):
        log_control_evaluation(tmp_path, evaluation())
    assert control_evaluations(tmp_path) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"tag": "release-1"},
        {"harness_commit": "--x"},
        {"tasks": -1},
        {"extra": 1},
        {"created": "yesterday"},
        {"created": "2026-10-22T10:00:00"},  # no time zone
        {"created": "2026-10-22T10:00:00+02:00"},  # not UTC
    ],
)
def test_malformed_evaluations_are_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        evaluation(**changes)


def test_the_control_log_schema_is_generated() -> None:
    assert SCHEMA_MODELS["control-log"] is ControlLog


def test_a_lock_being_deleted_on_windows_is_waited_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os  # noqa: PLC0415

    real_open = os.open
    refusals = [PermissionError(13, "Permission denied")]

    def windows_open(path: str, flags: int, *args: Any) -> int:
        if refusals and str(path).endswith("control.lock"):
            raise refusals.pop()
        return real_open(path, flags, *args)

    monkeypatch.setattr("saddlefit.storage.control_log.os.open", windows_open)
    monkeypatch.setattr("saddlefit.storage.control_log._WINDOWS", True)
    log_control_evaluation(tmp_path, evaluation())
    assert control_evaluations(tmp_path) == 1


def test_a_permission_problem_elsewhere_is_reported_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def denied(path: str, flags: int, *args: Any) -> int:
        attempts.append(path)
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("saddlefit.storage.control_log.os.open", denied)
    monkeypatch.setattr("saddlefit.storage.control_log._WINDOWS", False)
    with pytest.raises(EnvironmentFailure, match="cannot create the control-log lock"):
        log_control_evaluation(tmp_path, evaluation())
    assert len(attempts) == 1  # no retry while waiting for a lock that is not there
