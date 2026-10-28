"""``.saddlefit/journal/<entry-id>.json``: changes the fast loop found, on probation (B8).

A journal entry records one fast-loop rescue: the task, the change it composed, the probe
runs behind it and whether the composed change passed the task's check. The change itself
was only ever used in a temporary copy of the harness; it reaches the harness store only if
the middle loop later accepts it on held-out tasks.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from saddlefit.harness.proposals import Proposal
from saddlefit.storage.migrations import upgrade

JOURNAL_SCHEMA_VERSION: Final = 1
JOURNAL_KIND: Final = "journal-entry"
JOURNAL_DIR: Final = "journal"


class TaskRef(BaseModel):
    """The task a rescue was for: its text by hash, and the check that judged it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    check: str


class JournalEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    entry_id: str
    created: datetime
    task: TaskRef
    proposal: Proposal
    probe_run_ids: tuple[str, ...]
    passed: bool
    status: Literal["probation", "accepted", "rejected"]


def task_ref(text: str, check: str) -> TaskRef:
    return TaskRef(text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), check=check)


def write_entry(root: Path, entry: JournalEntry) -> Path:
    """Write ``entry`` atomically; an entry is never overwritten."""
    folder = root / JOURNAL_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{entry.entry_id}.json"
    if path.exists():
        raise FileExistsError(f"journal entry already exists: {path}")
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json(indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def read_entries(root: Path) -> list[JournalEntry]:
    """Every entry, oldest first (entry IDs are ULIDs)."""
    folder = root / JOURNAL_DIR
    if not folder.is_dir():
        return []
    entries = []
    for path in sorted(folder.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries.append(
            JournalEntry.model_validate(upgrade(JOURNAL_KIND, raw, JOURNAL_SCHEMA_VERSION))
        )
    return entries
