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

from saddlefit.errors import IntegrityViolation
from saddlefit.providers.base import (
    GenParams,
    Message,
    ModelClient,
    ModelInfo,
    ModelReply,
    ToolSpec,
)

log = logging.getLogger(__name__)


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


def _texts(
    messages: Sequence[Message], tools: Sequence[ToolSpec] | None
) -> Iterator[tuple[str, str]]:
    for i, message in enumerate(messages):
        yield f"messages[{i}].content", message.content
        for j, call in enumerate(message.tool_calls):
            yield f"messages[{i}].tool_calls[{j}]", json.dumps(call.arguments, ensure_ascii=False)
    for k, tool in enumerate(tools or ()):
        yield f"tools[{k}]", json.dumps(tool.model_dump(), ensure_ascii=False)


class LeakGuard:
    """Scans outgoing prompts for canaries; ``canaries`` maps each canary to its task ID."""

    def __init__(self, canaries: Mapping[str, str], incident_log: Path | None = None) -> None:
        self._canaries = dict(canaries)
        self._incident_log = incident_log

    def check(self, messages: Sequence[Message], tools: Sequence[ToolSpec] | None = None) -> None:
        tasks: list[str] = []
        locations: list[str] = []
        for where, text in _texts(messages, tools):
            for canary, task_id in self._canaries.items():
                if canary in text:
                    tasks.append(task_id)
                    locations.append(where)
        if tasks:
            unique_tasks = sorted(set(tasks))
            log_error = None
            try:
                self._record(unique_tasks, locations)
            except OSError as exc:  # the leak still aborts with exit code 2
                log_error = f"{type(exc).__name__}: {exc}"
            raise LeakDetected(unique_tasks, locations, log_error=log_error)

    def _record(self, task_ids: list[str], locations: list[str]) -> None:
        log.error("leak guard: canary found in prompt", extra={"tasks": task_ids, "at": locations})
        if self._incident_log is None:
            return
        self._incident_log.parent.mkdir(parents=True, exist_ok=True)
        entry = {
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
        self._inner = inner
        self._guard = guard

    def info(self) -> ModelInfo:
        return self._inner.info()

    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None,
        params: GenParams,
    ) -> ModelReply:
        self._guard.check(messages, tools)
        return self._inner.chat(messages, tools, params)
