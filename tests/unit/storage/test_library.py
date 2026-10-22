from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from saddlefit.schema_registry import SCHEMA_MODELS
from saddlefit.storage.library import (
    ChangeRecord,
    next_change_id,
    read_records,
    write_record,
)

# the B8 example
EXAMPLE: dict[str, Any] = {
    "schema_version": 1,
    "id": "2026-10-21-003",
    "proposal_id": "01JB2Z0C7H2P6S8E4F1A9B3D5G",
    "model": "qwen3:8b",
    "model_digest": "sha256:…",
    "level": "mechanics",
    "files": ["loop.yaml"],
    "harness_commit": "b4c5d6e",
    "status": "accepted",
    "train": {"before": 2, "after": 5, "tasks": 6, "runs_per_task": 3},
    "held_out": {"wins": 2, "losses": 0, "tasks": 6, "runs_per_task": 3},
    "context_tokens_added": 140,
}


def record(**changes: Any) -> ChangeRecord:
    return ChangeRecord.model_validate({**EXAMPLE, **changes})


def test_the_spec_example_validates() -> None:
    assert record().held_out is not None


def test_write_then_read_in_order(tmp_path: Path) -> None:
    write_record(tmp_path, record(id="2026-10-22-001"))
    write_record(tmp_path, record(id="2026-10-21-002", status="rejected", held_out=None))
    assert [r.id for r in read_records(tmp_path)] == ["2026-10-21-002", "2026-10-22-001"]


def test_a_record_is_never_overwritten(tmp_path: Path) -> None:
    write_record(tmp_path, record())
    with pytest.raises(FileExistsError):
        write_record(tmp_path, record(status="pruned"))
    assert read_records(tmp_path)[0].status == "accepted"


def test_ids_count_per_day(tmp_path: Path) -> None:
    day = date(2026, 10, 22)
    assert next_change_id(tmp_path, day) == "2026-10-22-001"
    write_record(tmp_path, record(id="2026-10-22-001"))
    write_record(tmp_path, record(id="2026-10-22-007"))
    write_record(tmp_path, record(id="2026-10-21-009"))
    assert next_change_id(tmp_path, day) == "2026-10-22-008"


def test_the_last_id_of_a_day_is_999(tmp_path: Path) -> None:
    write_record(tmp_path, record(id="2026-10-22-999"))
    with pytest.raises(ValueError, match="999"):
        next_change_id(tmp_path, date(2026, 10, 22))


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 2},
        {"id": "2026-10-22-1"},
        {"proposal_id": "ZZZZZZZZZZZZZZZZZZZZZZZZZZ"},
        {"status": "probation"},
        {"harness_commit": "--upload-pack"},
        {"train": {"before": -1, "after": 0, "tasks": 6, "runs_per_task": 3}},
        {"held_out": {"wins": 1, "losses": 0, "tasks": 6, "runs_per_task": 3, "task_ids": ["x"]}},
        {"extra": 1},
    ],
)
def test_malformed_records_are_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        record(**changes)


def test_the_change_record_schema_is_generated() -> None:
    assert SCHEMA_MODELS["change-record"] is ChangeRecord
