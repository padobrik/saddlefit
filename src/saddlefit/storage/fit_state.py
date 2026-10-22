"""``.saddlefit/fit.json``: how many middle-loop rounds have run, so ``fit`` resumes (A7).

Each round takes the next window of training tasks; without this count every ``fit`` would
start again at the first batch.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from saddlefit.storage.migrations import upgrade

FIT_STATE_SCHEMA_VERSION: Final = 1
FIT_STATE_KIND: Final = "fit-state"
FIT_STATE_FILE: Final = "fit.json"


class FitState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    rounds_done: int = Field(ge=0)


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
