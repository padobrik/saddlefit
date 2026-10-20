"""Write-only access to the sealed store, used when tasks are created (B3, B8).

Code outside ``saddlefit.eval`` may put sealed data in place but never read it back, so
this module has no read operations. Each task gets a random canary, written to
``canary.txt`` and embedded as a comment in ``check.sh`` and in the hidden tests.

Each task directory holds ``sealed.json`` with the layout's integer ``schema_version``
(model ``SealedTaskInfo``, schema ``schemas/sealed-task.schema.json``); the reader in
``saddlefit.eval.sealed`` refuses a missing or unsupported version.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from saddlefit.tasks.models import SEALED_SCHEMA_VERSION, TASK_ID_PATTERN, SealedTaskInfo

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

CANARY_PREFIX = "SADDLEFIT-CANARY-"
_TASK_ID = re.compile(TASK_ID_PATTERN)
# a replaced task waits under ".<id>~old" until the new one is in place; "~" never
# appears in a task id or a staging name, so the name cannot collide with either
RETIRED_SUFFIX = "~old"
CHECK_OUTPUT_DIR = "check_output"
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


@contextmanager
def task_lock(sealed_root: Path, task_id: str) -> Iterator[None]:
    """Hold the task's exclusive lock; replacing a task and recording its output both take it.

    The operating system drops the lock when the process ends, so a crash leaves none behind.
    """
    fd = os.open(sealed_root / f".{task_id}~lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if sys.platform == "win32":
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError:  # LK_LOCK gives up after about ten seconds
                    continue
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def retired_name(task_id: str) -> str:
    """Name of the directory that holds a task while a replace is under way."""
    return f".{task_id}{RETIRED_SUFFIX}"


def _safe_relative(path: str) -> PurePosixPath:
    # a native Windows path would read a backslash as a separator the check below misses
    if "\\" in path:
        raise SealingError(f"hidden test path must use POSIX separators: {path!r}")
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
        if not _TASK_ID.fullmatch(task_id):
            raise SealingError(f"invalid task id: {task_id!r}")
        if (target_commit is None) == (reference_patch is None):
            raise SealingError("give exactly one of target_commit (history) or reference_patch")
        rels = _checked_paths(hidden_tests)
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with task_lock(self._root, task_id):
            self._write_locked(
                task_id=task_id,
                check_sh=check_sh,
                hidden_tests=hidden_tests,
                rels=rels,
                target_commit=target_commit,
                reference_patch=reference_patch,
                replace=replace,
            )

    def _write_locked(
        self,
        *,
        task_id: str,
        check_sh: str,
        hidden_tests: dict[str, str],
        rels: dict[str, PurePosixPath],
        target_commit: str | None,
        reference_patch: str | None,
        replace: bool,
    ) -> None:
        task_dir = self._root / task_id
        retired = self._root / retired_name(task_id)
        _recover(task_dir, retired)
        if task_dir.exists() and not replace:
            raise SealingError(f"sealed task already exists: {task_id}")

        # Build the task beside its final place, then swap it in, so a failed write never
        # leaves a partial task or destroys the one being replaced.
        staging = Path(tempfile.mkdtemp(prefix=f".{task_id}.", dir=self._root))
        try:
            os.chmod(staging, 0o700)
            canary = new_canary()
            info = SealedTaskInfo(schema_version=SEALED_SCHEMA_VERSION)
            _write(staging / "sealed.json", info.model_dump_json() + "\n")
            _write(staging / "canary.txt", canary + "\n")
            _write(staging / "check.sh", _embed(check_sh, ".sh", canary), executable=True)
            for path, content in hidden_tests.items():
                rel = rels[path]
                _write(staging / "hidden_tests" / rel, _embed(content, rel.suffix, canary))
            if target_commit is not None:
                _write(staging / "target_commit", target_commit.strip() + "\n")
            if reference_patch is not None:
                _write(staging / "reference.patch", reference_patch)
            _swap_into_place(staging, task_dir, retired)
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def _checked_paths(hidden_tests: dict[str, str]) -> dict[str, PurePosixPath]:
    """Normalize hidden-test paths; reject duplicates and a file that is another's parent."""
    rels = {path: _safe_relative(path) for path in hidden_tests}
    seen: dict[PurePosixPath, str] = {}
    for path, rel in rels.items():
        if rel in seen:
            raise SealingError(f"hidden test paths collide: {seen[rel]!r} and {path!r}")
        seen[rel] = path
    for rel, path in seen.items():
        for parent in rel.parents:
            if parent in seen:
                raise SealingError(f"hidden test paths collide: {seen[parent]!r} and {path!r}")
    return rels


def _move_check_output(retired: Path, task_dir: Path) -> None:
    """Move earlier hidden-check output into the new task as one directory.

    The writer stays write-only: it renames the directory, never lists or opens its files.
    """
    source = retired / CHECK_OUTPUT_DIR
    target = task_dir / CHECK_OUTPUT_DIR
    if source.is_dir() and not source.is_symlink() and not target.exists():
        source.rename(target)


def _recover(task_dir: Path, retired: Path) -> None:
    """Finish a replace that stopped between its renames."""
    if not retired.exists():
        return
    if task_dir.exists():
        # the new task was installed; its output move or the cleanup was missed
        _move_check_output(retired, task_dir)
        shutil.rmtree(retired)
    else:
        retired.rename(task_dir)  # the old task was never replaced


def _swap_into_place(staging: Path, task_dir: Path, retired: Path) -> None:
    if not task_dir.exists():
        staging.rename(task_dir)
        return
    task_dir.rename(retired)
    # If the process stops here, readers still find the task under its retired name and
    # the next write restores it, so the old task is never lost.
    try:
        staging.rename(task_dir)
    except OSError:
        retired.rename(task_dir)
        raise
    # a stop from here on leaves the new task in place and the output under the retired
    # name; the next write moves it over (see _recover)
    _move_check_output(retired, task_dir)
    shutil.rmtree(retired)


def _write(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(content, encoding="utf-8", newline="\n")
    os.chmod(path, 0o700 if executable else 0o600)
