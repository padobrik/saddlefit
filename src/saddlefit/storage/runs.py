"""Run records: ``runs/<yyyy-mm-dd>/<run-id>.jsonl``, events then a summary (B8)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field
from ulid import ULID

from saddlefit.storage.migrations import upgrade

RUN_SCHEMA_VERSION: Final = 1
RUN_KIND = "run-record"
ULID_PATTERN = r"^[0-9A-HJKMNP-TV-Z]{26}$"

EventType = Literal["model_call", "tool_call", "compaction", "loop_warning", "finish_gate", "error"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class RunEvent(_Strict):
    """One step of a run."""

    schema_version: Literal[1]
    type: EventType
    time: datetime
    data: dict[str, Any] = Field(default_factory=dict)


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
    return str(ULID())


class RunWriter:
    """Appends events to one run's JSONL file and closes it with a summary."""

    def __init__(self, runs_root: Path, run_id: str, *, now: datetime | None = None) -> None:
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
        if self._closed:
            raise RuntimeError(f"run {self.run_id} already has a summary")
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(model.model_dump_json() + "\n")

    def event(self, type: EventType, data: dict[str, Any] | None = None) -> None:
        self._append(
            RunEvent(
                schema_version=RUN_SCHEMA_VERSION,
                type=type,
                time=datetime.now(UTC),
                data=data or {},
            )
        )

    def finish(self, summary: RunSummary) -> None:
        if summary.run_id != self.run_id:
            raise ValueError(f"summary is for run {summary.run_id}, not {self.run_id}")
        self._append(summary)
        self._closed = True


def read_run(path: Path) -> tuple[list[RunEvent], RunSummary]:
    """Read a run record, upgrading the previous schema version; the summary must be last."""
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not lines:
        raise ValueError(f"empty run record: {path}")
    records = [upgrade(RUN_KIND, line, RUN_SCHEMA_VERSION) for line in lines]
    *events, last = records
    if last.get("type") != "summary":
        raise ValueError(f"run record has no summary line: {path}")
    return [RunEvent.model_validate(e) for e in events], RunSummary.model_validate(last)


def find_runs(runs_root: Path) -> list[Path]:
    """All run records, oldest first (ULIDs sort by time)."""
    return sorted(runs_root.glob("*/*.jsonl"), key=lambda p: p.stem)
