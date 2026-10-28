"""``fit.json`` migrations (C7)."""

from __future__ import annotations

from typing import Final

from saddlefit.storage.migrations import Record, register

FIT_STATE_KIND: Final = "fit-state"


@register(FIT_STATE_KIND, 1)
def _v1_to_v2(record: Record) -> Record:
    """v2 counts refused tutor attempts; a v1 file counted none, so they start at zero."""
    return {**record, "schema_version": 2}
