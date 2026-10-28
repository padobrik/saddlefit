import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from saddlefit.schema_registry import SCHEMA_MODELS
from saddlefit.storage import fit_state as fit_state_module
from saddlefit.storage.fit_state import (
    FitLocked,
    FitState,
    fit_lock,
    load_fit_state,
    save_fit_state,
)
from saddlefit.storage.migrations import upgrade
from saddlefit.storage.migrations.reports import PROGRESS_KIND


def test_no_state_means_no_rounds(tmp_path: Path) -> None:
    assert load_fit_state(tmp_path).rounds_done == 0


def test_the_round_count_survives(tmp_path: Path) -> None:
    save_fit_state(tmp_path, FitState(schema_version=2, rounds_done=4))
    assert load_fit_state(tmp_path).rounds_done == 4
    assert [p.name for p in tmp_path.iterdir()] == ["fit.json"]  # no temporary file left


@pytest.mark.parametrize(
    "raw", ['{"schema_version": 1, "rounds_done": -1}', '{"schema_version": 1}']
)
def test_a_bad_state_is_refused(tmp_path: Path, raw: str) -> None:
    (tmp_path / "fit.json").write_text(raw)
    with pytest.raises(ValidationError):
        load_fit_state(tmp_path)


def test_the_fit_state_schema_is_generated() -> None:
    assert SCHEMA_MODELS["fit-state"] is FitState


def test_only_one_fit_holds_the_lock(tmp_path: Path) -> None:
    with (
        fit_lock(tmp_path),
        pytest.raises(FitLocked, match="another fit is running"),
        fit_lock(tmp_path),
    ):
        pass
    with fit_lock(tmp_path):  # released on exit
        pass
    assert not (tmp_path / "fit.lock").exists()


def test_the_lock_is_released_when_fit_fails(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), fit_lock(tmp_path):
        raise RuntimeError("round failed")
    assert not (tmp_path / "fit.lock").exists()


def test_a_v1_fit_json_reads_as_v2_with_no_refusals(tmp_path: Path) -> None:
    v1 = {
        "schema_version": 1,
        "rounds_done": 3,
        "accepted_since_ablation": 1,
        "student_model": "gemma4:e4b",
        "student_digest": "sha256:abc",
        "tutor_calls": 9,
        "tutor_usd": 1.25,
    }
    (tmp_path / "fit.json").write_text(json.dumps(v1), encoding="utf-8")
    state = load_fit_state(tmp_path)
    assert state.schema_version == 2
    assert (state.rounds_done, state.tutor_calls, state.tutor_usd) == (3, 9, 1.25)
    assert state.student_digest == "sha256:abc"
    assert state.refusals == {}


@pytest.mark.parametrize("kind", ["progress", "final"])
def test_a_v1_report_upgrades_with_no_refusals(kind: str) -> None:
    v1 = {"schema_version": 1, "kind": kind, "tutor": {"calls": 2, "usd": 0.3}}
    v2 = upgrade(PROGRESS_KIND, v1, 2)
    assert v2["tutor"] == {"calls": 2, "usd": 0.3, "refused": {}}
    assert (v2["kind"], v2["schema_version"]) == (kind, 2)


class _FakeFcntl:
    F_FULLFSYNC = 51

    def __init__(self, fails: bool) -> None:
        self.fails = fails
        self.calls: list[tuple[int, int]] = []

    def fcntl(self, fd: int, cmd: int) -> int:
        self.calls.append((fd, cmd))
        if self.fails:
            raise OSError("not supported")
        return 0


@pytest.mark.parametrize("fails", [False, True])
def test_a_save_flushes_past_the_drive_cache_when_it_can(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    fake = _FakeFcntl(fails)
    synced: list[int] = []
    monkeypatch.setattr(fit_state_module, "fcntl", fake)
    monkeypatch.setattr(fit_state_module.os, "fsync", synced.append)
    save_fit_state(tmp_path, FitState(schema_version=2, rounds_done=1))
    assert fake.calls  # F_FULLFSYNC was asked for
    assert all(cmd == _FakeFcntl.F_FULLFSYNC for _, cmd in fake.calls)
    # fsync only where full sync is not supported
    assert len(synced) == (len(fake.calls) if fails else 0)
    assert load_fit_state(tmp_path).rounds_done == 1
