"""Where a ``fit`` is, for the terminal and for ``saddlefit status`` (#113).

The loops report stages and finished runs to a ``ProgressSink``; they never touch the
terminal. Held-out runs reach it only as a count: no task ID and no outcome (B10).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from saddlefit.storage.progress import (
    PROGRESS_SCHEMA_VERSION,
    ProgressRecord,
    Stage,
    clear_progress,
    save_progress,
)


class ProgressSink(Protocol):
    def stage(self, name: Stage, candidate: int | None = None) -> None: ...

    def batch(self, runs: int) -> None: ...

    def tick(self) -> None: ...


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class FitProgress:
    """Keeps ``progress.json`` current and tells ``on_change`` about every change."""

    root: Path
    rounds: int
    candidates: int
    first_round: int = 1
    on_change: Callable[[ProgressRecord], None] | None = None
    clock: Callable[[], datetime] = _now
    pid: int = field(default_factory=os.getpid)
    record: ProgressRecord | None = None

    def start_round(self, number: int) -> None:
        """``number`` counts this command's rounds from 1."""
        self._update(round=number, stage="train", candidate=None, done=0, total=None)

    def stage(self, name: Stage, candidate: int | None = None) -> None:
        self._update(stage=name, candidate=candidate, done=0, total=None)

    def batch(self, runs: int) -> None:
        current = self._current()
        self._update(total=(current.total or 0) + runs)

    def tick(self) -> None:
        self._update(done=self._current().done + 1)

    def charged(self, calls: int, usd: float) -> None:
        self._update(tutor_calls=calls, tutor_usd=usd)

    def finish(self) -> None:
        """The command ended cleanly: nothing is left to report."""
        clear_progress(self.root)

    def _current(self) -> ProgressRecord:
        if self.record is None:
            now = self.clock()
            self.record = ProgressRecord(
                schema_version=PROGRESS_SCHEMA_VERSION,
                command="fit",
                pid=self.pid,
                started_at=now,
                updated_at=now,
                round=1,
                rounds=self.rounds,
                stage="train",
                candidates=self.candidates,
            )
        return self.record

    def _update(self, **changes: object) -> None:
        record = self._current().model_copy(update={**changes, "updated_at": self.clock()})
        self.record = ProgressRecord.model_validate(record.model_dump())
        save_progress(self.root, self.record)
        if self.on_change is not None:
            self.on_change(self.record)
