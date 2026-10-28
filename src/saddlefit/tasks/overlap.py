"""Which functions a patch changes, so a new task is not one already in the suite.

A symbol is ``path::Qualified.name`` for a function or class whose own code differs between
the old file and the patched one, or ``path`` for other top-level code that differs. Imports
do not count: two fixes that each add an import are not the same fix. A file that is not
Python, or does not parse, counts as a whole.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

_DEFS: Final = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
# the line endings Python itself counts: a form feed or U+2028 inside a string is no break
_LINES: Final = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$")


@dataclass
class RepeatGuard:
    """Which task in the suite, or kept in this command, a new one repeats.

    Two tasks that change the same function let a change learned on one pass the other's
    check, so a repeat that lands in another split makes the held-out gate measure nothing.
    """

    known: dict[str, frozenset[str]]
    pending: dict[str, frozenset[str]] = field(default_factory=dict)

    def repeats(self, symbols: frozenset[str]) -> str | None:
        for other, theirs in self.known.items():
            if common := symbols & theirs:
                return f"repeats {other}: both change {min(common)}"
        return None

    def check(self, task_id: str, patch: str, read_old: Callable[[str], str | None]) -> str | None:
        """The repeat ``patch`` makes, if any; its symbols wait for ``kept`` either way."""
        self.pending[task_id] = changed_symbols(patch, read_old)
        return self.repeats(self.pending[task_id])

    def kept(self, task_id: str) -> None:
        self.known[task_id] = self.pending.pop(task_id, frozenset())


def symbols_between(path: str, old: str | None, new: str | None) -> frozenset[str]:
    """The symbols that differ between two versions of one file."""
    return frozenset(_differences(path, old, new))


def patch_paths(patch: str) -> list[str]:
    """The files a ``git diff`` patch changes, from its ``diff --git`` headers."""
    paths = []
    for line in patch.splitlines():
        if line.startswith("diff --git a/"):
            path = line.split(" b/", 1)[-1]
            if path not in paths:
                paths.append(path)
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
    if old == new:
        return set()
    if not path.endswith(".py"):
        return {path}
    try:
        before, after = _own_code(old), _own_code(new)
    except SyntaxError:
        return {path}
    changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
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
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")
