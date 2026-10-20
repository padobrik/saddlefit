"""Which harness files the tutor and the fast loop may change (B8, hard rule 3)."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePosixPath

# Paths inside the harness store; everything else, saddlefit.yaml included, is off limits.
ALLOWED_FILES: frozenset[str] = frozenset(
    {"systemprompt.md", "tool_guidance.md", "loop.yaml", "EXPERIENCE.md"}
)
ALLOWED_DIRS: frozenset[str] = frozenset({"skills", "tools"})


def is_allowed(path: str) -> bool:
    """Whether ``path`` (relative to the harness store) may be changed by a proposal."""
    # Judge the raw segments: PurePosixPath would quietly normalize "./x" and "a//b".
    segments = path.split("/")
    if not path or any(seg in {"", ".", ".."} for seg in segments):
        return False
    rel = PurePosixPath(path)
    if rel.as_posix() in ALLOWED_FILES:
        return True
    return len(rel.parts) >= 2 and rel.parts[0] in ALLOWED_DIRS


def disallowed(paths: Iterable[str]) -> list[str]:
    """The paths a change may not touch, sorted; empty when the change is allowed."""
    return sorted({p for p in paths if not is_allowed(p)})
