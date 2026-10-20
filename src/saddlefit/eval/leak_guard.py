"""The leak guard: every prompt to any model is scanned for known canaries (B10, mech. 3).

A hit aborts the run with ``LeakDetected`` (exit code 2) and records an incident. The
incident names the task and where the canary appeared, never the canary itself.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from saddlefit.errors import IntegrityViolation
from saddlefit.eval.sealed import SealedStore, SealedStoreError
from saddlefit.providers.base import (
    GenParams,
    Message,
    ModelClient,
    ModelInfo,
    ModelReply,
    ToolSpec,
)

log = logging.getLogger(__name__)

INCIDENT_SCHEMA_VERSION = 1  # incidents.jsonl lines: schema_version, time, type, tasks, locations


class LeakDetected(IntegrityViolation):
    """A sealed canary was found in an outgoing prompt."""

    def __init__(
        self, task_ids: Sequence[str], locations: Sequence[str], *, log_error: str | None = None
    ) -> None:
        self.task_ids = tuple(task_ids)
        self.locations = tuple(locations)
        note = f"; incident log could not be written: {log_error}" if log_error else ""
        super().__init__(
            f"sealed data leaked into a prompt (tasks: {', '.join(self.task_ids)}; "
            f"at: {', '.join(self.locations)}); run aborted{note}"
        )


Location = tuple[str, ...]


def _strings(value: Any, where: Location) -> Iterator[tuple[Location, str]]:
    """Every raw string inside ``value``, keys included, with its location as path parts.

    Raw values are scanned, not their JSON: serialization could escape characters and
    hide a canary. Locations stay in parts so a key holding a canary can be redacted.
    """
    if isinstance(value, str):
        yield where, value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _strings(key, (*where, ".<key>"))
            yield from _strings(item, (*where, f".{key}"))
    elif isinstance(value, list | tuple):
        for i, item in enumerate(value):
            yield from _strings(item, (*where, f"[{i}]"))


def _texts(
    messages: Sequence[Message], tools: Sequence[ToolSpec] | None
) -> Iterator[tuple[Location, str]]:
    for i, message in enumerate(messages):
        yield (f"messages[{i}]", ".content"), message.content
        if message.tool_call_id is not None:  # sent to the provider too
            yield (f"messages[{i}]", ".tool_call_id"), message.tool_call_id
        for j, call in enumerate(message.tool_calls):
            yield from _strings(call.model_dump(), (f"messages[{i}]", f".tool_calls[{j}]"))
    for k, tool in enumerate(tools or ()):
        yield from _strings(tool.model_dump(), (f"tools[{k}]",))


class LeakGuard:
    """Scans outgoing prompts for canaries; ``canaries`` maps each canary to its task ID."""

    def __init__(self, canaries: Mapping[str, str], incident_log: Path | None = None) -> None:
        """``canaries`` maps every known canary to its task ID; incidents go to ``incident_log``."""
        self._canaries = dict(canaries)
        self._incident_log = incident_log

    def check(self, messages: Sequence[Message], tools: Sequence[ToolSpec] | None = None) -> None:
        """Raise ``LeakDetected`` if any canary appears anywhere in the outgoing prompt."""
        tasks: list[str] = []
        locations: list[str] = []
        for parts, text in _texts(messages, tools):
            for canary, task_id in self._canaries.items():
                if canary in text:
                    tasks.append(task_id)
                    where = self._render(parts)
                    if where not in locations:
                        locations.append(where)
        if tasks:
            unique_tasks = sorted(set(tasks))
            log_error = None
            try:
                self._record(unique_tasks, locations)
            except OSError as exc:  # the leak still aborts with exit code 2
                log_error = f"{type(exc).__name__}: {exc}"
            raise LeakDetected(unique_tasks, locations, log_error=log_error)

    def _render(self, parts: Location) -> str:
        """Join a location, hiding any part (such as a mapping key) that holds a canary."""
        return "".join(
            ".<redacted>" if any(c in part for c in self._canaries) else part for part in parts
        )

    def _record(self, task_ids: list[str], locations: list[str]) -> None:
        """Log the incident and append it to the incident log, without any canary."""
        log.error("leak guard: canary found in prompt", extra={"tasks": task_ids, "at": locations})
        if self._incident_log is None:
            return
        self._incident_log.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "schema_version": INCIDENT_SCHEMA_VERSION,
            "time": datetime.now(UTC).isoformat(),
            "type": "canary_leak",
            "tasks": task_ids,
            "locations": locations,
        }
        with self._incident_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


class GuardedClient:
    """A ``ModelClient`` that runs every prompt through the leak guard before sending it."""

    def __init__(self, inner: ModelClient, guard: LeakGuard) -> None:
        """Wrap ``inner`` so that ``guard`` checks every prompt first."""
        self._inner = inner
        self._guard = guard

    def info(self) -> ModelInfo:
        """The wrapped client's model info."""
        return self._inner.info()

    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None,
        params: GenParams,
    ) -> ModelReply:
        """Check the prompt, then forward it; a leak raises before anything is sent."""
        self._guard.check(messages, tools)
        return self._inner.chat(messages, tools, params)


def guard_for_suite(sealed_root: Path, incident_log: Path | None = None) -> LeakGuard:
    """A leak guard over every canary in ``tasks/sealed``, built inside ``saddlefit.eval``."""
    store = SealedStore(sealed_root)
    ids = [p.name for p in sorted(sealed_root.iterdir())] if sealed_root.is_dir() else []
    canaries = {}
    for task_id in ids:
        try:
            canaries[store.canary(task_id)] = task_id
        except (SealedStoreError, FileNotFoundError):  # staging leftovers, non-task entries
            continue
    return LeakGuard(canaries, incident_log=incident_log)
