"""Progress and final report migrations (C7)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from saddlefit.storage.migrations import Record, register

# Names the format, like "run-record": progress and final reports share it.
PROGRESS_KIND: Final = "progress-report"
BASELINE_KIND: Final = "baseline-report"


@register(PROGRESS_KIND, 1)
def _v1_to_v2(record: Record) -> Record:
    """v2 counts the tutor's refusals; a v1 report counted none."""
    tutor = record.get("tutor")
    if isinstance(tutor, dict):
        record = {**record, "tutor": {**tutor, "refused": tutor.get("refused", {})}}
    return {**record, "schema_version": 2}


@register(BASELINE_KIND, 1)
def _baseline_v1_to_v2(record: Record) -> Record:
    """v2 writes ``created`` with microseconds; v1 wrote whole seconds.

    A v1 time becomes the last moment of its second: the latest the report can have been
    written, and so the moment by which every baseline run had started.
    """
    created = record.get("created")
    if isinstance(created, str) and "." not in created.split("T", 1)[-1]:
        moment = datetime.fromisoformat(created) + timedelta(seconds=1, microseconds=-1)
        record = {**record, "created": moment.isoformat(timespec="microseconds")}
    return {**record, "schema_version": 2}
