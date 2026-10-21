"""``saddlefit.yaml`` migrations (C7)."""

from __future__ import annotations

from typing import Final

from saddlefit.storage.migrations import Record, register

CONFIG_KIND: Final = "config"


@register(CONFIG_KIND, 1)
def _v1_to_v2(record: Record) -> Record:
    """v2 adds the tutor's prompt-cache prices; a v1 file stated none, so they stay 0.0."""
    return {**record, "schema_version": 2}
