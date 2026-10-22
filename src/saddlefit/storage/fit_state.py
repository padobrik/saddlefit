"""``.saddlefit/fit.json``: how many middle-loop rounds have run, so ``fit`` resumes (A7).

Each round takes the next window of training tasks; without this count every ``fit`` would
start again at the first batch.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from saddlefit.errors import UserError
from saddlefit.storage.migrations import upgrade

FIT_STATE_SCHEMA_VERSION: Final = 1
FIT_STATE_KIND: Final = "fit-state"
FIT_STATE_FILE: Final = "fit.json"
FIT_LOCK_FILE: Final = "fit.lock"


class FitState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    rounds_done: int = Field(ge=0)
    # ablation runs after every 3 accepted changes and after a change of student model
    accepted_since_ablation: int = Field(default=0, ge=0)
    student_model: str | None = None
    student_digest: str | None = None  # the same name can point to new weights
    # tutor spending over every fit, for reports (B15)
    tutor_calls: int = Field(default=0, ge=0)
    tutor_usd: float = Field(default=0.0, ge=0.0)


def load_fit_state(root: Path) -> FitState:
    """The saved state, or zero rounds when ``fit`` has never finished one."""
    path = root / FIT_STATE_FILE
    if not path.exists():
        return FitState(schema_version=FIT_STATE_SCHEMA_VERSION, rounds_done=0)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return FitState.model_validate(upgrade(FIT_STATE_KIND, raw, FIT_STATE_SCHEMA_VERSION))


def save_fit_state(root: Path, state: FitState) -> None:
    """Replace the state atomically, so a crash leaves the old count, never a broken file."""
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(state.model_dump_json(indent=2) + "\n")
        os.replace(tmp, root / FIT_STATE_FILE)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class FitLocked(UserError):
    """Another ``fit`` is running on this project."""


def ensure_unlocked(root: Path) -> None:
    """Raise ``FitLocked`` while a fit holds the lock; takes nothing itself."""
    path = root / FIT_LOCK_FILE
    if path.exists():
        raise FitLocked(
            f"another fit is running on this project ({path}); if none is, remove that file"
        )


@contextlib.contextmanager
def fit_lock(root: Path) -> Iterator[None]:
    """Hold the project's fit lock; parallel fits would share the store and the round count.

    The lock is a file created exclusively and removed on exit. A crash can leave it behind;
    the error says how to clear it.
    """
    root.mkdir(parents=True, exist_ok=True)
    path = root / FIT_LOCK_FILE
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise FitLocked(
            f"another fit is running on this project ({path}); if none is, remove that file"
        ) from exc
    try:
        os.write(fd, f"{os.getpid()}\n".encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)
