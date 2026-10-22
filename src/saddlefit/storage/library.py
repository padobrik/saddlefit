"""Change records: ``library/<id>.json``, one per candidate the middle loop decided (B8).

A record keeps what was decided and the aggregate numbers behind it: training-batch counts
and held-out wins and losses. It never holds per-task held-out outcomes (B10).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class HeldOutCounts(_Strict):
    """Held-out aggregates only: wins (unsolved → solved) and losses (solved → unsolved)."""

    wins: int = Field(ge=0)
    losses: int = Field(ge=0)
    tasks: int = Field(ge=0)
    runs_per_task: int = Field(ge=1)


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


def library_dir(root: Path) -> Path:
    return root / "library"


def write_record(root: Path, record: ChangeRecord) -> Path:
    """Write a new record; an existing ID is never overwritten."""
    path = library_dir(root) / f"{record.id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(record.model_dump_json(indent=2) + "\n")
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
    return [read_record(p) for p in sorted(library_dir(root).glob("*.json"))]
