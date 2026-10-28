"""Progress and final report migrations (C7)."""

from __future__ import annotations

from typing import Final

from saddlefit.storage.migrations import Record, register

# Names the format, like "run-record": progress and final reports share it.
PROGRESS_KIND: Final = "progress-report"


@register(PROGRESS_KIND, 1)
def _v1_to_v2(record: Record) -> Record:
    """v2 counts the tutor's refusals; a v1 report counted none."""
    tutor = record.get("tutor")
    if isinstance(tutor, dict):
        record = {**record, "tutor": {**tutor, "refused": tutor.get("refused", {})}}
    return {**record, "schema_version": 2}
