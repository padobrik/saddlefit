"""``saddlefit status`` and the plain progress lines (#113)."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from saddlefit.cli import fit_progress_line, progress_status, status_text
from saddlefit.loops.progress import FitProgress
from saddlefit.project import Project
from saddlefit.storage.fit_state import FitState, save_fit_state
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


def test_a_dead_pid_shows_as_interrupted(tmp_path: Path) -> None:
    project = project_with(tmp_path)
    progress = tracker(project.state, pid=2**22 + 12345)  # no such process
    progress.start_round(1)
    progress.stage("candidate", 2)
    data = project.status()
    assert data["state"] == "interrupted"
    text = status_text(data, NOW)
    assert text.startswith("fit interrupted (pid 4206649 is gone) at round 1/2 · candidate 2/3")
    assert "started 58 min ago, last update 3 min ago" in text
    assert "fit.lock can be removed" in text


def test_a_live_pid_shows_as_running(tmp_path: Path) -> None:
    project = project_with(tmp_path)
    progress = tracker(project.state, pid=os.getpid())
    progress.start_round(1)
    assert project.status()["state"] == "running"
