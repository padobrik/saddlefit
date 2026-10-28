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


_ESCAPES = {'"': '\\"', "\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"}


def quoted(name: str) -> str:
    """``name`` as git writes it in a patch header: C-quoted when it has a special byte."""
    if not any(c in _ESCAPES or ord(c) < 0x20 or ord(c) == 0x7F for c in name):
        return name
    body = "".join(
        _ESCAPES.get(c, f"\\{ord(c):03o}" if ord(c) < 0x20 or ord(c) == 0x7F else c) for c in name
    )
    return f'"{body}"'


def file_diff(path: str, before: str | None, after: str) -> str:
    """The diff that turns ``before`` (``None`` for a new file) into ``after``; "" if equal."""
    if before == after:
        return ""
    old = "/dev/null" if before is None else quoted(f"a/{path}")
    new = quoted(f"b/{path}")
    body = _marked(
        difflib.unified_diff(_lines(before or ""), _lines(after), fromfile=old, tofile=new)
    )
    mode = "new file mode 100644\n" if before is None else ""
    return f"diff --git {quoted(f'a/{path}')} {new}\n{mode}{body}"
