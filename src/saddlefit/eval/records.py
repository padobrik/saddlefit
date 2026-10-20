"""Evaluator-side access to every run record, held-out and control included (B10).

Only ``saddlefit.eval`` may read held-out and control traces and per-task outcomes; other
packages use ``saddlefit.storage.runs.read_run``, which refuses them. A test keeps the
unrestricted reader inside this package.
"""

from __future__ import annotations

from pathlib import Path

from saddlefit.storage.runs import RunEvent, RunSummary, _read_run_any


def read_any_run(path: Path) -> tuple[list[RunEvent], RunSummary]:
    """Read a run record of any split; for the evaluator and its aggregate reports."""
    return _read_run_any(path)
