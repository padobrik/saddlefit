"""``.saddlefit/progress.json``: where a running command is, for ``saddlefit status`` (#113).

The file is replaced atomically on every stage change and finished run, and removed when
the command ends cleanly; one left behind with a dead process shows where a run stopped.
"""

from __future__ import annotations

import ctypes
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from saddlefit.storage.migrations import upgrade

PROGRESS_SCHEMA_VERSION: Final = 1
PROGRESS_KIND: Final = "progress"
PROGRESS_FILE: Final = "progress.json"

Stage = Literal["train", "tutor", "candidate", "held-out", "lessons", "ablation", "closing"]


class ProgressRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    command: Literal["fit"]
    pid: int = Field(gt=0)
    started_at: datetime
    updated_at: datetime
    round: int = Field(ge=1)
    rounds: int = Field(ge=1)
    stage: Stage
    candidate: int | None = Field(default=None, ge=1)
    candidates: int | None = Field(default=None, ge=1)
    done: int = Field(default=0, ge=0)
    total: int | None = Field(default=None, ge=0)
    tutor_calls: int = Field(default=0, ge=0)  # this command's
    tutor_usd: float = Field(default=0.0, ge=0.0)


def save_progress(root: Path, record: ProgressRecord) -> None:
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".tmp-progress-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(record.model_dump_json(indent=2) + "\n")
        os.replace(tmp, root / PROGRESS_FILE)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_progress(root: Path) -> ProgressRecord | None:
    path = root / PROGRESS_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    return ProgressRecord.model_validate(upgrade(PROGRESS_KIND, raw, PROGRESS_SCHEMA_VERSION))


def clear_progress(root: Path) -> None:
    (root / PROGRESS_FILE).unlink(missing_ok=True)


# Windows API values for process_alive
_QUERY_LIMITED_INFORMATION: Final = 0x1000
_ACCESS_DENIED: Final = 5
_STILL_ACTIVE: Final = 259


def process_alive(pid: int) -> bool:
    """Whether ``pid`` still runs; a process we may not inspect still counts as alive."""
    if os.name == "nt":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_process_alive(pid: int) -> bool:
    # os.kill(pid, 0) is no probe on Windows: 0 is CTRL_C_EVENT, sent to the process group
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        error: int = ctypes.get_last_error()  # type: ignore[attr-defined]
        return error == _ACCESS_DENIED
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # it exists; only its state is unknown
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)
