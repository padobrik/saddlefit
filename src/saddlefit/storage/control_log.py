"""``.saddlefit/control.json``: every control evaluation, logged and counted (B10, mech. 5).

Control tasks run only through ``report --final``, on a harness frozen with a tag; each such
evaluation is appended here, and every report prints how many there have been.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from saddlefit.errors import EnvironmentFailure
from saddlefit.storage.migrations import upgrade

CONTROL_LOG_SCHEMA_VERSION: Final = 1
CONTROL_LOG_KIND: Final = "control-log"
CONTROL_LOG_FILE: Final = "control.json"
CONTROL_LOG_LOCK: Final = "control.lock"
LOCK_WAIT_S: Final = 10.0


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ControlEvaluation(_Strict):
    tag: str = Field(pattern=r"^fit-\d{4}-\d{2}-\d{2}-\d+$")
    harness_commit: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    baseline_harness_commit: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    created: AwareDatetime
    tasks: int = Field(ge=0)

    @field_validator("created")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("control evaluation times are in UTC")
        return value


class ControlLog(_Strict):
    schema_version: Literal[1]
    evaluations: tuple[ControlEvaluation, ...] = ()


def load_control_log(root: Path) -> ControlLog:
    path = root / CONTROL_LOG_FILE
    if not path.exists():
        return ControlLog(schema_version=CONTROL_LOG_SCHEMA_VERSION)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ControlLog.model_validate(upgrade(CONTROL_LOG_KIND, raw, CONTROL_LOG_SCHEMA_VERSION))


@contextlib.contextmanager
def _locked(root: Path) -> Iterator[None]:
    """Hold ``control.lock`` across a read-modify-write, so no evaluation is ever lost."""
    path = root / CONTROL_LOG_LOCK
    deadline = time.monotonic() + LOCK_WAIT_S  # read at call time
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise EnvironmentFailure(
                    f"the control log is locked ({path}); if no saddlefit runs, remove that file"
                ) from None
            time.sleep(0.05)
    try:
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


def log_control_evaluation(root: Path, evaluation: ControlEvaluation) -> ControlLog:
    """Append one evaluation under the log's lock, replacing the file atomically for readers."""
    root.mkdir(parents=True, exist_ok=True)
    with _locked(root):
        log = load_control_log(root)
        log = log.model_copy(update={"evaluations": (*log.evaluations, evaluation)})
        fd, tmp = tempfile.mkstemp(dir=root, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(log.model_dump_json(indent=2) + "\n")
            os.replace(tmp, root / CONTROL_LOG_FILE)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return log


def control_evaluations(root: Path) -> int:
    """How many times control has been evaluated on this project."""
    return len(load_control_log(root).evaluations)
