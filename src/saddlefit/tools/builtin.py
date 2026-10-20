"""The student's tools (B6): read, list, search, edit, write, bash and finish.

Every tool runs inside the sandbox through ``exec``; nothing touches the host filesystem,
so a symlink in the workspace cannot reach host files. Paths are confined to ``/work``.
File contents travel as base64 arguments to a small in-container Python helper, so no
content is ever interpolated into a shell command.
"""

from __future__ import annotations

import base64
import inspect
import posixpath
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from saddlefit.providers.base import ToolSpec

WORKDIR = "/work"
DEFAULT_COMMAND_TIMEOUT_S = 120
MAX_WRITE_BYTES = 512_000


class Executor(Protocol):
    """What tools need from a sandbox: run a shell command in ``/work`` with a timeout."""

    def exec(self, command: str, timeout_s: float) -> Any: ...


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str
    finished: bool = False


class ToolError(ValueError):
    """Bad tool arguments; returned to the model as a failed tool call."""


def confine(path: str) -> str:
    """Resolve ``path`` inside ``/work``; refuse anything that would leave it."""
    if not path or "\x00" in path:
        raise ToolError("path must be a non-empty string")
    resolved = posixpath.normpath(posixpath.join(WORKDIR, path))
    if resolved != WORKDIR and not resolved.startswith(WORKDIR + "/"):
        raise ToolError(f"path is outside the workspace: {path}")
    return resolved


def truncate(text: str, max_chars: int) -> str:
    """Keep the head and tail of ``text``, with a note saying how much was cut."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    cut = len(text) - 2 * half
    return f"{text[:half]}\n[... {cut} characters cut; narrow the request ...]\n{text[-half:]}"


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


# Runs inside the sandbox: python3 -c HELPER <op> <b64 args...>; prints a status line first.
_HELPER = r"""
import base64, os, sys
op, *args = sys.argv[1:]
args = [base64.b64decode(a).decode("utf-8") for a in args]
if op == "edit":
    path, old, new = args
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    count = text.count(old)
    if count != 1:
        print(f"ERROR old text found {count} times; it must occur exactly once")
        sys.exit(1)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace(old, new, 1))
    print("OK edited")
elif op == "write":
    path, content = args
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"OK wrote {len(content)} characters")
"""


def _python(op: str, *args: str) -> str:
    return " ".join(["python3", "-c", shlex.quote(_HELPER), op, *(_b64(a) for a in args)])


@dataclass
class Toolbox:
    """Executes tool calls against one sandbox."""

    sandbox: Executor
    max_chars: int = 4000

    def _run(self, command: str, timeout_s: float = 30) -> ToolResult:
        result = self.sandbox.exec(command, timeout_s)
        note = "\n[timed out]" if result.timed_out else ""
        output = truncate(result.output + note, self.max_chars)
        return ToolResult(ok=result.exit_code == 0 and not result.timed_out, output=output)

    def read_file(self, path: str, start: int | None = None, end: int | None = None) -> ToolResult:
        target = shlex.quote(confine(path))
        if start is None and end is None:
            return self._run(f"cat -n -- {target}")
        first, last = max(1, start or 1), end if end is not None else "$"
        return self._run(f"cat -n -- {target} | sed -n '{first},{last}p'")

    def list_dir(self, path: str = ".") -> ToolResult:
        return self._run(f"ls -1Ap -- {shlex.quote(confine(path))}")

    def search(self, pattern: str, path: str = ".") -> ToolResult:
        target = shlex.quote(confine(path))
        return self._run(f"rg -n --max-columns 300 -e {shlex.quote(pattern)} -- {target}")

    def edit_file(self, path: str, old: str, new: str) -> ToolResult:
        if not old:
            raise ToolError("old text must not be empty; use write_file to create a file")
        return self._run(_python("edit", confine(path), old, new))

    def write_file(self, path: str, content: str) -> ToolResult:
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ToolError(f"content is larger than {MAX_WRITE_BYTES} bytes")
        return self._run(_python("write", confine(path), content))

    def bash(self, command: str, timeout: float | None = None) -> ToolResult:
        if not command.strip():
            raise ToolError("command must not be empty")
        return self._run(command, timeout_s=timeout or DEFAULT_COMMAND_TIMEOUT_S)

    def finish(self, summary: str) -> ToolResult:
        return ToolResult(ok=True, output=summary, finished=True)

    def call(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        """Dispatch a validated tool call; bad arguments come back as a failed result."""
        handler: Callable[..., ToolResult] | None = getattr(self, name, None)
        if name not in TOOL_NAMES or handler is None:
            return ToolResult(ok=False, output=f"unknown tool: {name}")
        try:
            inspect.signature(handler).bind(**arguments)
        except TypeError as exc:
            return ToolResult(ok=False, output=f"error: bad arguments for {name}: {exc}")
        try:
            return handler(**arguments)
        except ToolError as exc:
            return ToolResult(ok=False, output=f"error: {exc}")


def _spec(name: str, description: str, props: dict[str, Any], required: list[str]) -> ToolSpec:
    schema = {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }
    return ToolSpec(name=name, description=description, parameters=schema)


_STR = {"type": "string"}
_INT = {"type": "integer"}

TOOL_SPECS: tuple[ToolSpec, ...] = (
    _spec(
        "read_file",
        "Read a file, optionally lines start..end.",
        {"path": _STR, "start": _INT, "end": _INT},
        ["path"],
    ),
    _spec("list_dir", "List a directory.", {"path": _STR}, []),
    _spec(
        "search",
        "Search files with a regular expression.",
        {"pattern": _STR, "path": _STR},
        ["pattern"],
    ),
    _spec(
        "edit_file",
        "Replace text that occurs exactly once in a file.",
        {"path": _STR, "old": _STR, "new": _STR},
        ["path", "old", "new"],
    ),
    _spec(
        "write_file",
        "Create or overwrite a file.",
        {"path": _STR, "content": _STR},
        ["path", "content"],
    ),
    _spec(
        "bash",
        "Run a shell command in the workspace (no network).",
        {"command": _STR, "timeout": _INT},
        ["command"],
    ),
    _spec("finish", "End the task with a short summary.", {"summary": _STR}, ["summary"]),
)
TOOL_NAMES: frozenset[str] = frozenset(spec.name for spec in TOOL_SPECS)
