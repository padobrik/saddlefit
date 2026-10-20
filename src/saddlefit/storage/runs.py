"""Run records: ``runs/<yyyy-mm-dd>/<run-id>.jsonl``, events then a summary (B8)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ulid import ULID

from saddlefit.storage.migrations import upgrade

RUN_SCHEMA_VERSION: Final = 1
RUN_KIND = "run-record"
ULID_PATTERN = r"^[0-9A-HJKMNP-TV-Z]{26}$"

EventType = Literal["model_call", "tool_call", "compaction", "loop_warning", "finish_gate", "error"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ModelCallData(_Strict):
    """One model call: token usage and what came back."""

    step: int = Field(ge=1)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    tool_calls: tuple[str, ...] = ()
    malformed: int = Field(default=0, ge=0)
    repaired: bool = False


class ToolCallData(_Strict):
    """One tool execution; its output is not stored, only its size."""

    name: str
    ok: bool
    output_chars: int = Field(ge=0)


class CompactionData(_Strict):
    dropped_outputs: int = Field(ge=0)


class LoopWarningData(_Strict):
    signature: str
    stop: bool


class FinishGateData(_Strict):
    """The finish gate runs the task's VISIBLE check; its output is what the student saw."""

    passed: bool
    output: str


class ErrorData(_Strict):
    kind: str
    message: str


EventData = (
    ModelCallData | ToolCallData | CompactionData | LoopWarningData | FinishGateData | ErrorData
)

# Each event type has exactly one payload shape; unknown keys are rejected, so nothing else
# (hidden-check output in particular) can be written into a run record.
EVENT_DATA: dict[str, type[_Strict]] = {
    "model_call": ModelCallData,
    "tool_call": ToolCallData,
    "compaction": CompactionData,
    "loop_warning": LoopWarningData,
    "finish_gate": FinishGateData,
    "error": ErrorData,
}


def _bind_payload_to_type(schema: dict[str, Any]) -> None:
    """In the generated JSON Schema, require each event type's own payload (if/then)."""
    schema["allOf"] = [
        {
            "if": {"properties": {"type": {"const": event_type}}, "required": ["type"]},
            # payload models are flat, so their schemas are inlined (no $ref to resolve)
            "then": {"properties": {"data": model.model_json_schema()}},
        }
        for event_type, model in EVENT_DATA.items()
    ]


class RunEvent(_Strict):
    """One step of a run, with a payload typed by its event type."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, json_schema_extra=_bind_payload_to_type
    )

    schema_version: Literal[1]
    type: EventType
    time: datetime
    data: EventData

    @model_validator(mode="before")
    @classmethod
    def _typed_payload(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("type") in EVENT_DATA:
            payload = value.get("data", {})
            if not isinstance(payload, _Strict):
                payload = EVENT_DATA[value["type"]].model_validate(payload)
            value = {**value, "data": payload}
        return value

    @model_validator(mode="after")
    def _payload_matches_type(self) -> Self:
        if not isinstance(self.data, EVENT_DATA[self.type]):
            raise ValueError(f"{self.type} event needs a {EVENT_DATA[self.type].__name__} payload")
        return self


class ModelRef(_Strict):
    id: str
    digest: str | None = None
    runner: str | None = None
    tool_mode: Literal["native", "json"] | None = None


class CheckResult(_Strict):
    """The hidden check's exit code and duration only; its output stays sealed."""

    exit_code: int
    duration_s: float = Field(ge=0)


class DiffStats(_Strict):
    files: int = Field(ge=0)
    insertions: int = Field(ge=0)
    deletions: int = Field(ge=0)


class RunSummary(_Strict):
    """The last line of a run record."""

    schema_version: Literal[1]
    type: Literal["summary"]
    run_id: str = Field(pattern=ULID_PATTERN)
    task_id: str
    split: Literal["train", "held_out", "control"]
    attempt: int = Field(ge=1)
    model: ModelRef
    harness_commit: str
    saddlefit_version: str
    suite_hash: str
    sandbox_image: str
    seed: int
    steps: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    harness_tokens: int = Field(ge=0)
    wall_seconds: float = Field(ge=0)
    outcome: Literal["pass", "fail"]
    primary_tag: str
    tags: tuple[str, ...]
    check: CheckResult | None = None
    diff_stats: DiffStats | None = None


def new_run_id() -> str:
    """A new ULID: unique, and sortable by creation time."""
    return str(ULID())


class RunWriter:
    """Appends events to one run's JSONL file and closes it with a summary."""

    def __init__(self, runs_root: Path, run_id: str, *, now: datetime | None = None) -> None:
        """Create ``runs/<date>/<run_id>.jsonl`` exclusively; refuse if it exists."""
        # the id becomes a path component, and finish() needs a summary-valid id
        if not re.fullmatch(ULID_PATTERN, run_id):
            raise ValueError(f"run_id is not a ULID: {run_id!r}")
        when = now or datetime.now(UTC)
        self.run_id = run_id
        self.path = runs_root / when.strftime("%Y-%m-%d") / f"{run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # "x" checks and creates in one step, so two writers can never share a file.
            self.path.open("x", encoding="utf-8").close()
        except FileExistsError:
            raise FileExistsError(f"run record already exists: {self.path}") from None
        self._closed = False

    def _append(self, model: BaseModel) -> None:
        """Write one JSON line; nothing may follow the summary."""
        if self._closed:
            raise RuntimeError(f"run {self.run_id} already has a summary")
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(model.model_dump_json() + "\n")

    def event(self, type: EventType, data: dict[str, Any] | None = None) -> None:
        """Append an event; ``data`` must match the payload model for ``type``."""
        self._append(
            RunEvent(
                schema_version=RUN_SCHEMA_VERSION,
                type=type,
                time=datetime.now(UTC),
                data=cast("EventData", EVENT_DATA[type].model_validate(data or {})),
            )
        )

    def finish(self, summary: RunSummary) -> None:
        """Close the record with its summary line."""
        if summary.run_id != self.run_id:
            raise ValueError(f"summary is for run {summary.run_id}, not {self.run_id}")
        self._append(summary)
        self._closed = True


class RestrictedRunError(PermissionError):
    """A held-out or control run record was read outside ``saddlefit.eval`` (B10)."""


# Only training runs may reach the tutor, the fast loop or reports in detail (B10).
PUBLIC_SPLITS: Final = frozenset({"train"})


def _read_run_any(path: Path) -> tuple[list[RunEvent], RunSummary]:
    """Read any run record. Evaluator only: import it solely from ``saddlefit.eval``."""
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not lines:
        raise ValueError(f"empty run record: {path}")
    records = [upgrade(RUN_KIND, line, RUN_SCHEMA_VERSION) for line in lines]
    *events, last = records
    if last.get("type") != "summary":
        raise ValueError(f"run record has no summary line: {path}")
    return [RunEvent.model_validate(e) for e in events], RunSummary.model_validate(last)


def read_run(path: Path) -> tuple[list[RunEvent], RunSummary]:
    """Read a training run record; held-out and control records are refused.

    Their traces and per-task outcomes are visible only to the evaluator, which reads them
    through ``saddlefit.eval.records`` and reports aggregates.
    """
    events, summary = _read_run_any(path)
    if summary.split not in PUBLIC_SPLITS:
        raise RestrictedRunError(
            f"{summary.split} run records are read only by saddlefit.eval: {path.name}"
        )
    return events, summary


def find_runs(runs_root: Path) -> list[Path]:
    """All run records, oldest first (ULIDs sort by time)."""
    return sorted(runs_root.glob("*/*.jsonl"), key=lambda p: p.stem)
