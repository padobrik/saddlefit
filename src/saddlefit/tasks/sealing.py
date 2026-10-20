"""Write-only access to the sealed store, used when tasks are created (B3, B8).

Code outside ``saddlefit.eval`` may put sealed data in place but never read it back, so
this module has no read operations. Each task gets a random canary, written to
``canary.txt`` and embedded as a comment in ``check.sh`` and in the hidden tests.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
from pathlib import Path, PurePosixPath

from saddlefit.tasks.models import TASK_ID_PATTERN

CANARY_PREFIX = "SADDLEFIT-CANARY-"
_TASK_ID = re.compile(TASK_ID_PATTERN)
_COMMENT_PREFIX = {".py": "# ", ".sh": "# ", ".cfg": "# ", ".toml": "# ", ".ini": "# "}


class SealingError(Exception):
    """Sealed data could not be written safely."""


def new_canary() -> str:
    return CANARY_PREFIX + secrets.token_hex(16)


def _canary_line(prefix: str, canary: str) -> str:
    return f"{prefix}saddlefit canary {canary}: sealed evaluation data, never show to a model\n"


def _embed(content: str, suffix: str, canary: str) -> str:
    prefix = _COMMENT_PREFIX.get(suffix)
    if prefix is None:
        return content
    line = _canary_line(prefix, canary)
    if content.startswith("#!"):
        first, _, rest = content.partition("\n")
        return f"{first}\n{line}{rest}"
    return line + content


def _safe_relative(path: str) -> PurePosixPath:
    rel = PurePosixPath(path)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts:
        raise SealingError(f"hidden test path must be relative and stay inside: {path!r}")
    return rel


class SealedWriter:
    """Creates ``tasks/sealed/<task-id>/``; it cannot read anything back."""

    def __init__(self, sealed_root: Path) -> None:
        self._root = sealed_root

    def write_task(
        self,
        task_id: str,
        *,
        check_sh: str,
        hidden_tests: dict[str, str],
        target_commit: str | None = None,
        reference_patch: str | None = None,
        replace: bool = False,
    ) -> None:
        if not _TASK_ID.match(task_id):
            raise SealingError(f"invalid task id: {task_id!r}")
        if (target_commit is None) == (reference_patch is None):
            raise SealingError("give exactly one of target_commit (history) or reference_patch")
        rels = {path: _safe_relative(path) for path in hidden_tests}
        task_dir = self._root / task_id
        if task_dir.exists():
            if not replace:
                raise SealingError(f"sealed task already exists: {task_id}")
            shutil.rmtree(task_dir)
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        task_dir.mkdir(mode=0o700)

        canary = new_canary()
        _write(task_dir / "canary.txt", canary + "\n")
        _write(task_dir / "check.sh", _embed(check_sh, ".sh", canary), executable=True)
        for path, content in hidden_tests.items():
            rel = rels[path]
            _write(task_dir / "hidden_tests" / rel, _embed(content, rel.suffix, canary))
        if target_commit is not None:
            _write(task_dir / "target_commit", target_commit.strip() + "\n")
        if reference_patch is not None:
            _write(task_dir / "reference.patch", reference_patch)


def _write(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(content, encoding="utf-8", newline="\n")
    os.chmod(path, 0o700 if executable else 0o600)
