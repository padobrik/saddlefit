"""Read access to the sealed store: evaluator only (B3, B10).

Nothing outside ``saddlefit.eval`` may import this module; a test enforces it. Hidden
tests, ``check.sh``, reference solutions, canaries and hidden-check output are read here
and nowhere else.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from saddlefit.tasks.models import SEALED_SCHEMA_VERSION, TASK_ID_PATTERN
from saddlefit.tasks.sealing import (
    CANARY_PREFIX,
    CHECK_OUTPUT_DIR,
    RETIRED_SUFFIX,
    retired_name,
    task_lock,
)

_TASK_ID = re.compile(TASK_ID_PATTERN)
_RUN_ID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")  # a ULID


class SealedStoreError(Exception):
    """A sealed task is missing or malformed."""


def _check_format(task_dir: Path, task_id: str) -> None:
    """Accept only a sealed-task layout this version can read (``sealed.json``)."""
    try:
        info = json.loads((task_dir / "sealed.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        info = {}
    except (OSError, ValueError) as exc:
        raise SealedStoreError(f"unreadable sealed.json for task {task_id!r}") from exc
    version = info.get("schema_version") if isinstance(info, dict) else None
    # bool is an int subclass, so test the exact type
    if type(version) is not int:
        raise SealedStoreError(f"task {task_id!r}: missing or non-integer schema_version")
    if version != SEALED_SCHEMA_VERSION:
        raise SealedStoreError(
            f"task {task_id!r}: schema_version {version} is not supported "
            f"(current {SEALED_SCHEMA_VERSION})"
        )


class SealedStore:
    """The evaluator's view of ``tasks/sealed/``."""

    def __init__(self, sealed_root: Path) -> None:
        self._root = sealed_root

    def _task_dir(self, task_id: str) -> Path:
        if not _TASK_ID.fullmatch(task_id):
            raise SealedStoreError(f"invalid task id: {task_id!r}")
        path = self._root / task_id
        if path.parent != self._root:
            raise SealedStoreError(f"no sealed data for task {task_id!r}")
        if not path.exists() and not path.is_symlink():
            # a replace interrupted between its renames leaves the task retired, not gone
            path = self._root / retired_name(task_id)
        # is_dir() follows symlinks, and a linked task could point anywhere
        if path.is_symlink() or not path.is_dir():
            raise SealedStoreError(f"no sealed data for task {task_id!r}")
        _check_format(path, task_id)
        return path

    def check_script(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "check.sh"

    def hidden_tests_dir(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "hidden_tests"

    def hidden_test_paths(self, task_id: str) -> list[str]:
        """Paths of the hidden tests relative to the repository root, sorted."""
        root = self.hidden_tests_dir(task_id)
        if not root.is_dir():
            return []
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())

    def target_commit(self, task_id: str) -> str | None:
        path = self._task_dir(task_id) / "target_commit"
        return path.read_text(encoding="utf-8").strip() if path.exists() else None

    def reference_patch(self, task_id: str) -> str | None:
        path = self._task_dir(task_id) / "reference.patch"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def canary(self, task_id: str) -> str:
        value = (self._task_dir(task_id) / "canary.txt").read_text(encoding="utf-8").strip()
        if not value.startswith(CANARY_PREFIX):
            raise SealedStoreError(f"malformed canary for task {task_id!r}")
        return value

    def all_canaries(self) -> frozenset[str]:
        """Every known canary; the leak guard scans prompts for all of them."""
        if not self._root.is_dir():
            return frozenset()
        ids: set[str] = set()
        for p in self._root.iterdir():
            name = p.name
            if name.startswith(".") and name.endswith(RETIRED_SUFFIX):
                name = name[1 : -len(RETIRED_SUFFIX)]
            # skips staging directories (".<id>.*") left by an interrupted write
            if _TASK_ID.fullmatch(name) and p.is_dir():
                ids.add(name)
        return frozenset(
            self.canary(task_id)
            for task_id in sorted(ids)
            if (self._task_dir(task_id) / "canary.txt").exists()
        )

    def record_check_output(self, task_id: str, run_id: str, output: str) -> Path:
        """Store hidden-check output in the sealed store; it never enters a run record."""
        if not _RUN_ID.fullmatch(run_id):
            raise SealedStoreError(f"invalid run id: {run_id!r}")
        # the lock file is named after the id, so check it first; the directory itself is
        # resolved under the lock, where a replace cannot be moving it
        if not _TASK_ID.fullmatch(task_id):
            raise SealedStoreError(f"invalid task id: {task_id!r}")
        if not self._root.is_dir():
            raise SealedStoreError(f"no sealed data for task {task_id!r}")
        # a replace copies the output directory into the new task; the lock keeps a run
        # from landing in the old copy after that
        with task_lock(self._root, task_id):
            return self._record_locked(task_id, run_id, output)

    def _record_locked(self, task_id: str, run_id: str, output: str) -> Path:
        out_dir = self._task_dir(task_id) / CHECK_OUTPUT_DIR
        if out_dir.is_symlink():
            raise SealedStoreError(f"check_output of task {task_id!r} must not be a symlink")
        out_dir.mkdir(exist_ok=True, mode=0o700)
        path = out_dir / f"{run_id}.txt"
        if path.is_symlink():
            raise SealedStoreError(f"check output {path.name} must not be a symlink")
        # O_NOFOLLOW also refuses a link created after the check above (not on Windows)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8") as fh:
            fh.write(output)
        path.chmod(0o600)
        return path
