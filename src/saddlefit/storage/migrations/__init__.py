"""Migrations between schema versions of persisted formats (C7).

Each persisted kind registers one function per version step, ``from_version`` to
``from_version + 1``. Readers accept the current version and the previous one, and
upgrade the previous one through its registered migration.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Record = dict[str, Any]
Migration = Callable[[Record], Record]

_REGISTRY: dict[str, dict[int, Migration]] = {}


class MigrationError(ValueError):
    """A record's schema version is unsupported or cannot be upgraded."""


def register(kind: str, from_version: int) -> Callable[[Migration], Migration]:
    """Register ``fn`` as the upgrade of ``kind`` from ``from_version`` to the next one."""

    def decorate(fn: Migration) -> Migration:
        steps = _REGISTRY.setdefault(kind, {})
        if from_version in steps:
            raise MigrationError(f"duplicate migration for {kind} v{from_version}")
        steps[from_version] = fn
        return fn

    return decorate


def upgrade(kind: str, record: Record, current: int) -> Record:
    """Return ``record`` at ``current``; only the current and the previous version are read."""
    version = record.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise MigrationError(f"{kind}: missing or non-integer schema_version")
    if version == current:
        return record
    if version != current - 1:
        raise MigrationError(
            f"{kind}: schema_version {version} is not supported (current {current}; "
            f"readers accept {current} and {current - 1})"
        )
    step = _REGISTRY.get(kind, {}).get(version)
    if step is None:
        raise MigrationError(f"{kind}: no migration from v{version} to v{current}")
    upgraded = step(dict(record))
    if upgraded.get("schema_version") != current:
        raise MigrationError(f"{kind}: migration from v{version} did not produce v{current}")
    return upgraded
