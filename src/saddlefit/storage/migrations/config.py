"""``saddlefit.yaml`` migrations (C7)."""

from __future__ import annotations

from typing import Final

from saddlefit.storage.migrations import Record, register

CONFIG_KIND: Final = "config"


@register(CONFIG_KIND, 2)
def _v2_to_v3(record: Record) -> Record:
    """v3 adds the generator's budget and prices; a v2 file stated none, so defaults apply."""
    return {**record, "schema_version": 3}
