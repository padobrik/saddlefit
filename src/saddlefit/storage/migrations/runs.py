"""Run record migrations (C7)."""

from __future__ import annotations

from typing import Final

from saddlefit.storage.migrations import Record, register

RUN_KIND: Final = "run-record"


@register(RUN_KIND, 1)
def _v1_to_v2(record: Record) -> Record:
    """v2 adds the arguments of each tool call; a v1 record logged none, so they stay empty."""
    return {**record, "schema_version": 2}
