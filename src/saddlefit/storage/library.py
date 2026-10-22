"""Change records: ``library/<id>.json``, one per candidate the middle loop decided (B8).

A record keeps what was decided and the aggregate numbers behind it: training-batch counts
and held-out wins and losses. It never holds per-task held-out outcomes (B10).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from saddlefit.storage.migrations import upgrade

CHANGE_SCHEMA_VERSION: Final = 1
CHANGE_KIND: Final = "change-record"
# Crockford base32; a first digit above 7 would overflow 128 bits
_ULID: Final = r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"
_CHANGE_ID: Final = r"^\d{4}-\d{2}-\d{2}-\d{3}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class TrainCounts(_Strict):
    """Training-batch tasks solved before and after the change, on the same seeds."""

    before: int = Field(ge=0)
    after: int = Field(ge=0)
    tasks: int = Field(ge=1)
    runs_per_task: int = Field(ge=1)

    @model_validator(mode="after")
    def _within_tasks(self) -> Self:
        if max(self.before, self.after) > self.tasks:
            raise ValueError(f"solved counts cannot exceed the {self.tasks} batch tasks")
        return self


class HeldOutCounts(_Strict):
    """Held-out aggregates only: wins (unsolved → solved) and losses (solved → unsolved)."""

    wins: int = Field(ge=0)
    losses: int = Field(ge=0)
    tasks: int = Field(ge=0)
    runs_per_task: int = Field(ge=1)

    @model_validator(mode="after")
    def _within_tasks(self) -> Self:
        if self.wins + self.losses > self.tasks:
            raise ValueError(f"wins and losses cannot exceed the {self.tasks} held-out tasks")
        return self


class ChangeRecord(_Strict):
    schema_version: Literal[1]
    id: str = Field(pattern=_CHANGE_ID)
    proposal_id: str = Field(pattern=_ULID)
    model: str = Field(min_length=1)
    model_digest: str | None = None
    level: Literal["mechanics", "tools", "text", "reasoning"]
    files: tuple[str, ...]
    harness_commit: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    status: Literal["accepted", "rejected", "pruned"]
    train: TrainCounts
    held_out: HeldOutCounts | None = None  # None when the batch gate already rejected it
    context_tokens_added: int

    @field_validator("id")
    @classmethod
    def _real_day_and_number(cls, value: str) -> str:
        """A calendar date and a number from 001 to 999, as ``next_change_id`` makes."""
        day, number = value[:10], int(value[11:])
        date.fromisoformat(day)  # raises ValueError for a date like 2026-99-99
        if not 1 <= number <= 999:
            raise ValueError(f"change number must be 001 to 999, got {value[11:]}")
        return value

    @model_validator(mode="after")
    def _accepted_needs_held_out(self) -> Self:
        if self.status == "accepted" and self.held_out is None:
            raise ValueError("an accepted change passed the held-out gate, so it has results")
        return self


def library_dir(root: Path) -> Path:
    return root / "library"


def write_record(root: Path, record: ChangeRecord) -> Path:
    """Write a new record; an existing ID is never overwritten, and no partial file appears.

    The record is written to a temporary file first and published with a hard link, which
    fails if the ID exists.
    """
    path = library_dir(root) / f"{record.id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(record.model_dump_json(indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(tmp, path)  # FileExistsError if the ID is taken
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    return path


def next_change_id(root: Path, day: date) -> str:
    """The next free ``<yyyy-mm-dd>-<nnn>`` for ``day``."""
    prefix = day.isoformat()
    taken = [
        int(p.stem.rsplit("-", 1)[1])
        for p in library_dir(root).glob(f"{prefix}-[0-9][0-9][0-9].json")
    ]
    number = max(taken, default=0) + 1
    if number > 999:
        raise ValueError(f"more than 999 change records on {prefix}")
    return f"{prefix}-{number:03d}"


def read_record(path: Path) -> ChangeRecord:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ChangeRecord.model_validate(upgrade(CHANGE_KIND, raw, CHANGE_SCHEMA_VERSION))


def read_records(root: Path) -> list[ChangeRecord]:
    """Every change record, oldest first (IDs sort by date, then number)."""
    return [read_record(p) for p in sorted(library_dir(root).glob("[0-9]*.json"))]
