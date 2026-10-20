"""Persisted task-suite formats: ``meta.yaml`` and ``suite.lock`` (B3, B8, B10)."""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

TASK_SCHEMA_VERSION: Final = 1
SUITE_SCHEMA_VERSION: Final = 1

Split = Literal["train", "held_out", "control"]
Source = Literal["history", "mutation", "generated", "user"]
TASK_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,127}$"
# Control sets use only tasks from history or written by the user (A11, B13).
CONTROL_SOURCES: frozenset[str] = frozenset({"history", "user"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class RepoRef(_Strict):
    """Where the task's base snapshot comes from."""

    url: str | None = None
    base_commit: str = Field(pattern=r"^[0-9a-f]{7,64}$")


class TaskMeta(_Strict):
    """``tasks/public/<id>/meta.yaml``: what the student may know about a task."""

    schema_version: Literal[1]
    id: str = Field(pattern=TASK_ID_PATTERN)
    source: Source
    type: str = Field(min_length=1)
    split: Split
    repo: RepoRef
    visible_check: str | None = None
    created: date


class LockedTask(_Strict):
    """One task's entry in the lock: its split never changes once recorded."""

    id: str = Field(pattern=TASK_ID_PATTERN)
    split: Split
    source: Source
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class SplitFractions(_Strict):
    """Fractions recorded at init; later configs may not change them."""

    train: float = Field(ge=0.0, le=1.0)
    held_out: float = Field(ge=0.0, le=1.0)
    control: float = Field(ge=0.0, le=1.0)


class SuiteLock(_Strict):
    """``tasks/suite.lock``: task IDs, content hashes, splits and the split salt."""

    schema_version: Literal[1]
    salt: str = Field(pattern=r"^[0-9a-f]{64}$")
    salt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    fractions: SplitFractions
    tasks: tuple[LockedTask, ...] = ()

    def suite_hash(self) -> str:
        """A digest of every task's ID, split and content, independent of order."""
        h = hashlib.sha256()
        for task in sorted(self.tasks, key=lambda t: t.id):
            h.update(f"{task.id}\0{task.split}\0{task.content_hash}\n".encode())
        return f"sha256:{h.hexdigest()}"
