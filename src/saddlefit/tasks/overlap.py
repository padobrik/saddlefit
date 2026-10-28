"""Which functions a patch changes, so a new task is not one already in the suite.

A symbol is ``path::Qualified.name`` for a function or class whose own code differs between
the old file and the patched one, or ``path`` for other top-level code that differs. Imports
do not count: two fixes that each add an import are not the same fix. A file that is not
Python, or does not parse, counts as a whole.
"""

from __future__ import annotations

import ast
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Final

from saddlefit.tasks.mining import _git_env

_DEFS: Final = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


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
            ["git", "apply", "--whitespace=nowarn", "-"],  # noqa: S607
            cwd=root,
            env=_git_env(),
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
    lines = source.splitlines(keepends=True)
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
