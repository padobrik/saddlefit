"""``saddlefit status`` and the plain progress lines (#113)."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from saddlefit import project as project_module
from saddlefit.cli import fit_progress_line, progress_change, progress_status, status_text
from saddlefit.loops.progress import FitProgress
from saddlefit.project import Project
from saddlefit.storage.fit_state import FitState, fit_lock, save_fit_state
from saddlefit.storage.progress import load_progress

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def tracker(root: Path, pid: int = 4242) -> FitProgress:
    times = iter([NOW - timedelta(minutes=58), *[NOW - timedelta(minutes=3)] * 20])
    return FitProgress(root=root, rounds=2, candidates=3, pid=pid, clock=lambda: next(times))


def test_the_status_line_names_round_candidate_stage_and_cost(tmp_path: Path) -> None:
    progress = tracker(tmp_path)
    progress.start_round(1)
    progress.stage("held-out", 1)
    progress.batch(6)
    for _ in range(4):
        progress.tick()
    progress.charged(1, 0.84)
    record = load_progress(tmp_path)
    assert record is not None
    assert progress_status(record) == "round 1/2 · candidate 1/3 · held-out 4/6 · $0.84"
    line = fit_progress_line(record)
    assert "\r" not in line
    assert "\x1b" not in line


def project_with(tmp_path: Path) -> Project:
    project = Project(tmp_path)
    save_fit_state(
        project.state, FitState(schema_version=2, rounds_done=1, tutor_calls=3, tutor_usd=2.41)
    )
    return project


def test_status_is_idle_without_a_progress_file(tmp_path: Path) -> None:
    data = project_with(tmp_path).status()
    assert data["state"] == "idle"
    assert status_text(data, NOW) == "idle; all fits: 1 rounds, 3 tutor calls, $2.41"


def test_a_dead_pid_shows_as_interrupted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(project_module, "process_alive", lambda pid: False)
    project = project_with(tmp_path)
    progress = tracker(project.state, pid=4206649)
    progress.start_round(1)
    progress.stage("candidate", 2)
    (project.state / "fit.lock").write_text("4206649\n", encoding="utf-8")  # left by a crash
    data = project.status()
    assert data["state"] == "interrupted"
    text = status_text(data, NOW)
    assert text.startswith("fit interrupted (pid 4206649) at round 1/2 · candidate 2/3")
    assert "started 58 min ago, last update 3 min ago" in text
    assert text.endswith("fit.lock is left by pid 4206649, which is gone; it can be removed")


def test_a_lock_another_command_holds_is_never_called_removable(tmp_path: Path) -> None:
    project = project_with(tmp_path)
    progress = tracker(project.state, pid=4206649)  # an earlier fit that failed
    progress.start_round(1)
    with fit_lock(project.state):  # this process: a prune, say
        text = status_text(project.status(), NOW)
    assert text.endswith(f"fit.lock is held by pid {os.getpid()} (a fit, prune or report --final)")
    assert "removed" not in text


def test_no_lock_means_no_lock_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(project_module, "process_alive", lambda pid: False)
    project = project_with(tmp_path)
    tracker(project.state, pid=4206649).start_round(1)
    assert "fit.lock" not in status_text(project.status(), NOW)


def test_a_fit_holding_the_lock_shows_as_running(tmp_path: Path) -> None:
    project = project_with(tmp_path)
    progress = tracker(project.state, pid=os.getpid())
    progress.start_round(1)
    with fit_lock(project.state):  # it writes this process's pid
        assert project.status()["state"] == "running"


def test_a_live_pid_without_the_lock_is_interrupted(tmp_path: Path) -> None:
    # a failed fit drops the lock but keeps progress.json, and its pid may be reused
    project = project_with(tmp_path)
    progress = tracker(project.state, pid=os.getpid())
    progress.start_round(1)
    assert project.status()["state"] == "interrupted"


def test_only_a_finished_run_counts_as_progress_and_a_batch_size_prints_nothing(
    tmp_path: Path,
) -> None:
    seen: list[tuple[bool, bool]] = []
    last: list[object] = [None]

    def on_change(record: object) -> None:
        seen.append(progress_change(last[0], record))  # type: ignore[arg-type]
        last[0] = record

    times = iter([NOW] * 20)
    progress = FitProgress(
        root=tmp_path, rounds=1, candidates=2, on_change=on_change, clock=lambda: next(times)
    )
    progress.start_round(1)  # first record: a line, no run yet
    progress.stage("tutor", 1)  # a new stage: a line, still no finished run
    progress.stage("candidate", 1)
    progress.batch(6)  # the batch's size alone: no line
    progress.tick()  # a run finished
    assert seen == [(False, True), (False, True), (False, True), (False, False), (True, True)]
