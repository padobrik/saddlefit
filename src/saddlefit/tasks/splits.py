"""Deterministic split assignment and the suite lock (B10, mechanism 1).

A task's split is HMAC-SHA256(salt, task_id) mapped onto the configured fractions. The
salt is created once and stored in ``suite.lock``; a recorded split never changes, and
adding tasks never moves existing ones.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from saddlefit.config import SplitsConfig
from saddlefit.errors import UserError
from saddlefit.tasks.models import (
    CONTROL_SOURCES,
    SUITE_SCHEMA_VERSION,
    LockedTask,
    Source,
    Split,
    SplitFractions,
    SuiteLock,
)

_SCALE = float(2**64)


class SplitError(UserError):
    """A split would change, or the lock and the configuration disagree."""


def split_point(salt: bytes, task_id: str) -> float:
    """The task's position in [0, 1), from the first 8 bytes of HMAC-SHA256(salt, id)."""
    digest = hmac.new(salt, task_id.encode("utf-8"), hashlib.sha256).digest()
    return int.from_bytes(digest[:8], "big") / _SCALE


def assign_split(salt: bytes, task_id: str, fractions: SplitFractions, source: Source) -> Split:
    """Map a task onto a split; sources not allowed in control map onto train and held-out."""
    u = split_point(salt, task_id)
    if source not in CONTROL_SOURCES:
        usable = fractions.train + fractions.held_out
        if usable <= 0:
            raise SplitError(f"no train or held-out fraction for {source} task {task_id}")
        return "train" if u * usable < fractions.train else "held_out"
    if u < fractions.train:
        return "train"
    if u < fractions.train + fractions.held_out:
        return "held_out"
    return "control"


def new_lock(splits: SplitsConfig) -> SuiteLock:
    """A fresh lock with a random 256-bit salt, created by ``saddlefit init``."""
    salt = secrets.token_hex(32)
    return SuiteLock(
        schema_version=SUITE_SCHEMA_VERSION,
        salt=salt,
        salt_digest=f"sha256:{hashlib.sha256(bytes.fromhex(salt)).hexdigest()}",
        fractions=SplitFractions(**splits.model_dump()),
    )


def check_fractions(lock: SuiteLock, splits: SplitsConfig) -> None:
    """Refuse a configuration whose fractions differ from the ones the lock was made with."""
    if lock.fractions.model_dump() != splits.model_dump():
        raise SplitError(
            "split fractions in saddlefit.yaml differ from suite.lock; "
            "splits are fixed at init and never change"
        )


def _salt(lock: SuiteLock) -> bytes:
    salt = bytes.fromhex(lock.salt)
    if hashlib.sha256(salt).hexdigest() != lock.salt_digest.removeprefix("sha256:"):
        raise SplitError("suite.lock salt does not match its digest")
    return salt


def split_for(lock: SuiteLock, task_id: str, source: Source) -> Split:
    """The split a task has, or would get if it were added now."""
    for task in lock.tasks:
        if task.id == task_id:
            return task.split
    return assign_split(_salt(lock), task_id, lock.fractions, source)


def add_task(lock: SuiteLock, task_id: str, source: Source, content_hash: str) -> SuiteLock:
    """Record a task; an existing task keeps its split and source, and only its hash updates."""
    salt = _salt(lock)
    existing = {t.id: t for t in lock.tasks}
    if task_id in existing:
        old = existing[task_id]
        if old.source != source:
            raise SplitError(f"task {task_id} was recorded as {old.source}, not {source}")
        entry = LockedTask(id=old.id, split=old.split, source=old.source, content_hash=content_hash)
    else:
        split = assign_split(salt, task_id, lock.fractions, source)
        entry = LockedTask(id=task_id, split=split, source=source, content_hash=content_hash)
    existing[task_id] = entry
    tasks = tuple(sorted(existing.values(), key=lambda t: t.id))
    # Validate the whole lock again: model_copy(update=...) would skip validation.
    return SuiteLock.model_validate({**lock.model_dump(), "tasks": [t.model_dump() for t in tasks]})
