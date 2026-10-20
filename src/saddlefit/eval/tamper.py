"""Tamper detection: does a diff touch protected paths or the hidden tests' paths? (B9, B10)"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

_DIFF_HEADER = re.compile(r"^diff --git (?P<a>\S+) (?P<b>\S+)$")
_FILE_LINE = re.compile(r"^(?:---|\+\+\+) (?P<path>\S+)")
_RENAME = re.compile(r"^(?:rename|copy) (?:from|to) (?P<path>.+)$")


def _strip_prefix(path: str) -> str | None:
    if path == "/dev/null":
        return None
    if path.startswith(('"a/', '"b/')):
        path = path[1:-1]
    return path[2:] if path.startswith(("a/", "b/")) else path


def diff_paths(diff: str) -> set[str]:
    """Every path a unified git diff reads or writes: old and new names of each file."""
    paths: set[str] = set()
    for line in diff.splitlines():
        for regex in (_DIFF_HEADER, _FILE_LINE, _RENAME):
            match = regex.match(line)
            if match is None:
                continue
            for raw in match.groupdict().values():
                path = _strip_prefix(raw) if regex is not _RENAME else raw
                if path:
                    paths.add(path)
    return paths


@lru_cache(maxsize=256)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob: ``**`` spans directories, ``*`` and ``?`` stay within one."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(_glob_regex(p).match(path) for p in patterns)


def tampered_paths(
    touched: Iterable[str], protected: Iterable[str], hidden: Iterable[str]
) -> list[str]:
    """Touched paths that are protected or that coincide with a hidden test's path."""
    protected = tuple(protected)
    hidden_set = set(hidden)
    return sorted(p for p in set(touched) if p in hidden_set or matches_any(p, protected))
