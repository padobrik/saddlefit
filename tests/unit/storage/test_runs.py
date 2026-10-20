import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

import saddlefit
from saddlefit.eval.records import read_any_run
from saddlefit.storage import migrations
from saddlefit.storage.migrations import MigrationError, register, upgrade
from saddlefit.storage.runs import (
    RUN_KIND,
    ErrorData,
    RestrictedRunError,
    RunEvent,
    RunSummary,
    RunWriter,
    find_runs,
    new_run_id,
    read_run,
)

NOW = datetime(2026, 10, 21, 3, 0, tzinfo=UTC)


def summary(run_id: str, **overrides: Any) -> RunSummary:
    data: dict[str, Any] = {
        "schema_version": 1,
        "type": "summary",
        "run_id": run_id,
        "task_id": "fix-date-parser-0042",
        "split": "train",
        "attempt": 2,
        "model": {
            "id": "qwen3:8b",
            "digest": "sha256:abc",
            "runner": "ollama",
            "tool_mode": "native",
        },
        "harness_commit": "a1b2c3d",
        "saddlefit_version": "0.1.0",
        "suite_hash": "sha256:def",
        "sandbox_image": "sha256:123",
        "seed": 8,
        "steps": 23,
        "prompt_tokens": 18431,
        "completion_tokens": 2210,
        "harness_tokens": 3120,
        "wall_seconds": 182.4,
        "outcome": "fail",
        "primary_tag": "gave_up",
        "tags": ["gave_up"],
        "check": {"exit_code": 1, "duration_s": 12.3},
        "diff_stats": {"files": 2, "insertions": 14, "deletions": 3},
    }
    data.update(overrides)
    return RunSummary.model_validate(data)


@pytest.fixture
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migrations, "_REGISTRY", {})


def test_write_and_read_round_trip(tmp_path: Path) -> None:
    run_id = new_run_id()
    writer = RunWriter(tmp_path, run_id, now=NOW)
    writer.event("model_call", {"step": 1, "prompt_tokens": 100, "completion_tokens": 9})
    writer.event("tool_call", {"name": "read_file", "ok": True, "output_chars": 12})
    writer.finish(summary(run_id))
    assert writer.path == tmp_path / "2026-10-21" / f"{run_id}.jsonl"
    events, result = read_run(writer.path)
    assert [e.type for e in events] == ["model_call", "tool_call"]
    assert result == summary(run_id)
    assert find_runs(tmp_path) == [writer.path]


def test_nothing_after_the_summary(tmp_path: Path) -> None:
    run_id = new_run_id()
    writer = RunWriter(tmp_path, run_id, now=NOW)
    with pytest.raises(ValueError, match="summary is for run"):
        writer.finish(summary(new_run_id()))
    writer.finish(summary(run_id))
    with pytest.raises(RuntimeError, match="already has a summary"):
        writer.event("error", {"kind": "x", "message": "y"})
    with pytest.raises(FileExistsError):
        RunWriter(tmp_path, run_id, now=NOW)


def test_hidden_check_output_cannot_enter_a_run_record() -> None:
    """Part of required test 13: the summary keeps only exit code and duration."""
    with pytest.raises(ValidationError, match="output"):
        summary(new_run_id(), check={"exit_code": 1, "duration_s": 1.0, "output": "FAILED x"})
    with pytest.raises(ValidationError):
        summary(new_run_id(), hidden_output="FAILED x")


def test_reader_requires_a_summary_last(tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "type": "error",
                "time": NOW.isoformat(),
                "data": {"kind": "x", "message": "y"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no summary"):
        read_run(path)
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        read_run(path)


@pytest.mark.usefixtures("isolated_registry")
def test_previous_version_is_upgraded(tmp_path: Path) -> None:
    @register(RUN_KIND, 0)
    def v0_to_v1(record: dict[str, Any]) -> dict[str, Any]:
        if record.get("type") == "summary":
            record["harness_tokens"] = record.pop("context_tokens")
        return {**record, "schema_version": 1}

    run_id = new_run_id()
    old = summary(run_id).model_dump(mode="json")
    old["schema_version"] = 0
    old["context_tokens"] = old.pop("harness_tokens")
    path = tmp_path / "old.jsonl"
    path.write_text(json.dumps(old) + "\n", encoding="utf-8")
    _, result = read_run(path)
    assert result.harness_tokens == 3120


@pytest.mark.usefixtures("isolated_registry")
@pytest.mark.parametrize(
    ("record", "message"),
    [
        ({"schema_version": 5}, "not supported"),
        ({"schema_version": -1}, "not supported"),
        ({}, "missing or non-integer"),
        ({"schema_version": True}, "missing or non-integer"),
        ({"schema_version": 0}, "no migration"),
    ],
)
def test_unsupported_versions_are_rejected(record: dict[str, Any], message: str) -> None:
    with pytest.raises(MigrationError, match=message):
        upgrade(RUN_KIND, record, 1)


@pytest.mark.usefixtures("isolated_registry")
def test_registry_guards() -> None:
    register("k", 0)(lambda r: {**r, "schema_version": 1})
    with pytest.raises(MigrationError, match="duplicate"):
        register("k", 0)(lambda r: r)
    register("bad", 0)(lambda r: r)
    with pytest.raises(MigrationError, match="did not produce"):
        upgrade("bad", {"schema_version": 0}, 1)


@pytest.mark.parametrize("bad", ["not-a-ulid", "../escape", "01J/2", ""])
def test_writer_rejects_a_non_ulid_run_id_before_touching_disk(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="not a ULID"):
        RunWriter(tmp_path, bad, now=NOW)
    assert list(tmp_path.iterdir()) == []


def test_second_writer_for_the_same_run_is_refused_before_any_event(tmp_path: Path) -> None:
    run_id = new_run_id()
    first = RunWriter(tmp_path, run_id, now=NOW)
    assert first.path.exists()
    with pytest.raises(FileExistsError, match="already exists"):
        RunWriter(tmp_path, run_id, now=NOW)


@pytest.mark.parametrize(
    ("event_type", "data", "message"),
    [
        (
            "finish_gate",
            {"passed": False, "output": "1 failed", "hidden_output": "x"},
            "hidden_output",
        ),
        (
            "tool_call",
            {"name": "bash", "ok": True, "output_chars": 3, "output": "secret"},
            "output",
        ),
        ("model_call", {"step": 1}, "prompt_tokens"),
    ],
    ids=["extra-key-on-gate", "tool-output-not-stored", "missing-fields"],
)
def test_event_payloads_are_typed(
    tmp_path: Path, event_type: str, data: dict[str, Any], message: str
) -> None:
    writer = RunWriter(tmp_path, new_run_id(), now=NOW)
    with pytest.raises(ValidationError, match=message):
        writer.event(event_type, data)  # type: ignore[arg-type]


def test_payload_must_match_the_event_type() -> None:
    record = {
        "schema_version": 1,
        "type": "compaction",
        "time": NOW.isoformat(),
        "data": {"kind": "x", "message": "y"},
    }
    with pytest.raises(ValidationError):
        RunEvent.model_validate(record)
    with pytest.raises(ValidationError, match="needs a CompactionData"):
        RunEvent(
            schema_version=1, type="compaction", time=NOW, data=ErrorData(kind="x", message="y")
        )


def test_typed_events_round_trip(tmp_path: Path) -> None:
    run_id = new_run_id()
    writer = RunWriter(tmp_path, run_id, now=NOW)
    writer.event("finish_gate", {"passed": False, "output": "1 failed"})
    writer.event("loop_warning", {"signature": "read_file:abc", "stop": True})
    writer.event("compaction", {"dropped_outputs": 2})
    writer.finish(summary(run_id))
    events, _ = read_run(writer.path)
    assert [type(e.data).__name__ for e in events] == [
        "FinishGateData",
        "LoopWarningData",
        "CompactionData",
    ]


@pytest.mark.parametrize("split", ["held_out", "control"])
def test_held_out_and_control_records_are_refused_outside_eval(tmp_path: Path, split: str) -> None:
    run_id = new_run_id()
    writer = RunWriter(tmp_path, run_id, now=NOW)
    writer.finish(summary(run_id, split=split))
    with pytest.raises(RestrictedRunError, match=r"read only by saddlefit\.eval"):
        read_run(writer.path)
    events, result = read_any_run(writer.path)
    assert (events, result.split) == ([], split)


def test_only_eval_imports_the_unrestricted_reader() -> None:
    root = Path(saddlefit.__file__).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith("eval/") or rel == "storage/runs.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [a.name for a in node.names]
                if (node.module or "").endswith("eval.records") or (
                    node.module == "saddlefit.eval" and "records" in names
                ):
                    names.append("read_any_run")  # the module itself grants the access
            elif isinstance(node, ast.Import):
                names = ["read_any_run" for a in node.names if a.name == "saddlefit.eval.records"]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names = [node.value]
            if {"_read_run_any", "read_any_run"} & set(names):
                offenders.append(rel)
    assert offenders == []


def test_generated_schema_binds_each_event_type_to_its_payload() -> None:
    schema = RunEvent.model_json_schema()
    validator = jsonschema.Draft202012Validator(schema)
    good = {
        "schema_version": 1,
        "type": "compaction",
        "time": NOW.isoformat(),
        "data": {"dropped_outputs": 2},
    }
    mismatched = {**good, "data": {"kind": "x", "message": "y"}}
    assert validator.is_valid(good)
    RunEvent.model_validate(good)
    assert not validator.is_valid(mismatched)
    with pytest.raises(ValidationError):
        RunEvent.model_validate(mismatched)
