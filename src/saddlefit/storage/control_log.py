"""``.saddlefit/control.json``: every control evaluation, logged and counted (B10, mech. 5).

Control tasks run only through ``report --final``, on a harness frozen with a tag; each such
evaluation is appended here, and every report prints how many there have been.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from saddlefit.storage.migrations import upgrade

CONTROL_LOG_SCHEMA_VERSION: Final = 1
CONTROL_LOG_KIND: Final = "control-log"
CONTROL_LOG_FILE: Final = "control.json"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ControlEvaluation(_Strict):
    tag: str = Field(pattern=r"^fit-\d{4}-\d{2}-\d{2}-\d+$")
    harness_commit: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    baseline_harness_commit: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    created: str  # ISO 8601, UTC
    tasks: int = Field(ge=0)


class ControlLog(_Strict):
    schema_version: Literal[1]
    evaluations: tuple[ControlEvaluation, ...] = ()


def load_control_log(root: Path) -> ControlLog:
    path = root / CONTROL_LOG_FILE
    if not path.exists():
        return ControlLog(schema_version=CONTROL_LOG_SCHEMA_VERSION)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ControlLog.model_validate(upgrade(CONTROL_LOG_KIND, raw, CONTROL_LOG_SCHEMA_VERSION))


def log_control_evaluation(root: Path, evaluation: ControlEvaluation) -> ControlLog:
    """Append one evaluation and replace the log atomically; return the new log."""
    log = load_control_log(root)
    log = log.model_copy(update={"evaluations": (*log.evaluations, evaluation)})
    root.mkdir(parents=True, exist_ok=True)
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
