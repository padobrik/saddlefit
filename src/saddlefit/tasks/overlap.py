"""Which functions a patch changes, so a new task is not one already in the suite.

A symbol is ``path::Qualified.name`` for a function or class whose own code differs between
the old file and the patched one, or ``path`` for other top-level code that differs. Imports
do not count: two fixes that each add an import are not the same fix. A file that is not
Python, or does not parse, counts as a whole.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from saddlefit.storage.migrations import upgrade

_DEFS: Final = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
# the line endings Python itself counts: a form feed or U+2028 inside a string is no break
_LINES: Final = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$")


REPEAT_INDEX_SCHEMA_VERSION: Final = 1
REPEAT_INDEX_KIND: Final = "repeat-index"


class RepeatIndex(BaseModel):
    """The functions each kept generated or mutated task changes.

    Written when the task is kept, from the candidate's own reference patch before it was
    sealed, so the repeat check never reads the sealed store.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    tasks: dict[str, list[str]] = Field(default_factory=dict)


def load_repeat_index(path: Path) -> RepeatIndex:
    """The recorded symbols, or none when no generated task was kept yet."""
    if not path.exists():
        return RepeatIndex(schema_version=REPEAT_INDEX_SCHEMA_VERSION)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return RepeatIndex.model_validate(upgrade(REPEAT_INDEX_KIND, raw, REPEAT_INDEX_SCHEMA_VERSION))


def save_repeat_index(path: Path, index: RepeatIndex) -> None:
    """Replace the index atomically: a crash leaves the old one, never a broken file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-repeats-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(index.model_dump_json(indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass
class RepeatGuard:
    """Which task in the suite, or kept in this command, a new one repeats.

    Two tasks that change the same function let a change learned on one pass the other's
    check, so a repeat that lands in another split makes the held-out gate measure nothing.
    """

    known: dict[str, frozenset[str]]
    pending: dict[str, frozenset[str]] = field(default_factory=dict)

    def repeats(self, symbols: frozenset[str]) -> str | None:
        """The first known task that changes one of ``symbols``, and that symbol."""
        for other, theirs in self.known.items():
            if common := symbols & theirs:
                return f"repeats {other}: both change {min(common)}"
        return None

    def check(self, task_id: str, patch: str, read_old: Callable[[str], str | None]) -> str | None:
        """The repeat ``patch`` makes, if any; its symbols wait for ``kept`` either way."""
        self.pending[task_id] = changed_symbols(patch, read_old)
        return self.repeats(self.pending[task_id])

    def kept(self, task_id: str) -> None:
        """Record a kept task's symbols, so later candidates must not repeat it either."""
        self.known[task_id] = self.pending.pop(task_id, frozenset())

    def index(self, adding: str | None = None) -> RepeatIndex:
        """The known tasks, plus ``adding`` from pending, without recording it yet."""
        known = dict(self.known)
        if adding is not None:
            known[adding] = self.pending.get(adding, frozenset())
        return RepeatIndex(
            schema_version=REPEAT_INDEX_SCHEMA_VERSION,
            tasks={task_id: sorted(symbols) for task_id, symbols in sorted(known.items())},
        )


def symbols_between(path: str, old: str | None, new: str | None) -> frozenset[str]:
    """The symbols that differ between two versions of one file."""
    return frozenset(_differences(path, old, new))


def patch_paths(patch: str) -> list[str]:
    """The files a patch changes, by git's own parser, so quoted names are read right."""
    with tempfile.TemporaryDirectory(prefix="saddlefit-numstat-") as tmp:
        listed = subprocess.run(
            ["git", "apply", "--numstat", "-z", "-"],  # noqa: S607
            cwd=tmp,
            env=_isolated_git(Path(tmp)),
            input=patch.encode("utf-8"),
            capture_output=True,
            check=False,
        )
    if listed.returncode != 0:
        return []
    paths: list[str] = []
    fields = listed.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    i = 0
    while i < len(fields):
        parts = fields[i].split("\t", 2)
        # a rename or copy leaves the third part empty; both names follow as their own fields
        names = [parts[2]] if len(parts) == 3 and parts[2] else fields[i + 1 : i + 3]
        i += 1 if len(parts) == 3 and parts[2] else 3
        paths += [name for name in names if name and name not in paths]
    return paths


def changed_symbols(patch: str, read_old: Callable[[str], str | None]) -> frozenset[str]:
    """The symbols ``patch`` changes, against the old files that ``read_old`` returns."""
    paths = patch_paths(patch)
    if not paths:
        return frozenset()
    with tempfile.TemporaryDirectory(prefix="saddlefit-overlap-") as tmp:
        root = Path(tmp)
        old = {path: read_old(path) for path in paths}
        for path, text in old.items():
            if text is not None:
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_bytes(text.encode("utf-8"))
        applied = subprocess.run(
            ["git", "-c", "core.autocrlf=false", "apply", "--whitespace=nowarn", "-"],  # noqa: S607
            cwd=root,
            env=_isolated_git(root),
            input=patch.encode("utf-8"),
            capture_output=True,
            check=False,
        )
        if applied.returncode != 0:
            return frozenset(paths)  # cannot tell which functions: the files as a whole
        symbols: set[str] = set()
        for path in paths:
            target = root / path
            new = target.read_bytes().decode("utf-8") if target.is_file() else None
            symbols |= _differences(path, old[path], new)
    return frozenset(symbols)


def _isolated_git(root: Path) -> Mapping[str, str]:
    """No user or system config (Git for Windows turns on autocrlf), no enclosing repository."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CEILING_DIRECTORIES": str(root.parent),
    }


def _differences(path: str, old: str | None, new: str | None) -> set[str]:
    """The symbols of one file whose own code differs; the whole file when it is not Python."""
    if old == new:
        return set()
    if not path.endswith(".py"):
        return {path}
    try:
        before, after = _own_code(old), _own_code(new)
    except SyntaxError:
        return {path}
    changed = {
        name for name in before.keys() | after.keys() if before.get(name, "") != after.get(name, "")
    }
    return {f"{path}::{name}" if name else path for name in changed}


def _own_code(source: str | None) -> dict[str, str]:
    """Each function and class's own lines (nested ones excluded); "" for the top level."""
    if source is None:
        return {}
    tree = ast.parse(source)
    lines = _LINES.findall(source)
    owned: dict[str, set[int]] = {"": set(range(1, len(lines) + 1))}
    for node in tree.body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            owned[""] -= set(range(node.lineno, (node.end_lineno or node.lineno) + 1))

    def visit(parent: ast.AST, prefix: str, owner: str) -> None:
        """Give each definition its lines, taking them away from the one around it."""
        for child in ast.iter_child_nodes(parent):
            if not isinstance(child, _DEFS):
                continue
            name = f"{prefix}{child.name}"
            start = min([d.lineno for d in child.decorator_list] + [child.lineno])
            span = set(range(start, (child.end_lineno or child.lineno) + 1))
            owned[owner] -= span
            owned.setdefault(name, set()).update(span)
            visit(child, f"{name}.", name)

    visit(tree, "", "")
    # blank and comment-only lines change no behaviour; new functions bring new blank lines
    return {
        name: "".join(line for i in sorted(span) if _code(line := lines[i - 1]))
        for name, span in owned.items()
    }


def _code(line: str) -> bool:
    """Whether a line can change behaviour: not blank and not only a comment."""
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")
