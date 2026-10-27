"""Git-style unified diffs built from two texts, without calling git (B8).

``git apply`` reads what these write: ``diff --git`` headers, ``new file mode`` for a file
that did not exist, and the ``\\ No newline at end of file`` marker after a last line that
has no newline.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable

NO_NEWLINE = "\\ No newline at end of file\n"


def _lines(text: str) -> list[str]:
    """Lines as git sees them: split at LF only, each LF kept, a last line without one too."""
    return re.findall(r"[^\n]*\n|[^\n]+$", text)


def _marked(lines: Iterable[str]) -> str:
    return "".join(line if line.endswith("\n") else f"{line}\n{NO_NEWLINE}" for line in lines)


def file_diff(path: str, before: str | None, after: str) -> str:
    """The diff that turns ``before`` (``None`` for a new file) into ``after``; "" if equal."""
    if before == after:
        return ""
    old = "/dev/null" if before is None else f"a/{path}"
    body = _marked(
        difflib.unified_diff(_lines(before or ""), _lines(after), fromfile=old, tofile=f"b/{path}")
    )
    mode = "new file mode 100644\n" if before is None else ""
    return f"diff --git a/{path} b/{path}\n{mode}{body}"
