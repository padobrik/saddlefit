"""The task suite on disk: ``.saddlefit/tasks/`` with public, sealed and ``suite.lock`` (B3).

``materialize`` turns a viable mined commit into a task: the public part (``task.md`` and
``meta.yaml``) for the student, and the sealed part (the commit's tests, ``check.sh`` and a
canary) for the evaluator only. The base snapshot is the commit's parent.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from saddlefit.config import SplitsConfig
from saddlefit.tasks.mining import Candidate, _git_bytes, task_id_for, task_type_for
from saddlefit.tasks.models import TASK_SCHEMA_VERSION, RepoRef, SuiteLock, TaskMeta
from saddlefit.tasks.sealing import SealedWriter
from saddlefit.tasks.splits import add_task, check_fractions, new_lock

PYTEST = "PYTHONPATH=src:. python -B -m pytest -q -p no:cacheprovider"
_PR_SUFFIX = re.compile(r"\s*\(#\d+\)$")


@dataclass(frozen=True)
class SuitePaths:
    root: Path

    @property
    def public_dir(self) -> Path:
        return self.root / "public"

    @property
    def sealed_dir(self) -> Path:
        return self.root / "sealed"

    @property
    def lock(self) -> Path:
        return self.root / "suite.lock"


def load_lock(paths: SuitePaths, splits: SplitsConfig) -> SuiteLock:
    """Read ``suite.lock``, or start one (with a fresh salt) if the suite is new."""
    if not paths.lock.exists():
        return new_lock(splits)
    lock = SuiteLock.model_validate_json(paths.lock.read_text(encoding="utf-8"))
    check_fractions(lock, splits)
    return lock


def save_lock(paths: SuitePaths, lock: SuiteLock) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.lock.write_text(lock.model_dump_json(indent=2) + "\n", encoding="utf-8")


def load_meta(paths: SuitePaths, task_id: str) -> TaskMeta:
    raw = yaml.safe_load((paths.public_dir / task_id / "meta.yaml").read_text(encoding="utf-8"))
    return TaskMeta.model_validate(raw)


def _show(repo: Path, ref: str, rel: str) -> bytes | None:
    try:
        return _git_bytes(repo, "show", f"{ref}:{rel}")
    except subprocess.CalledProcessError:  # the path does not exist at ``ref``
        return None


def materialize(
    paths: SuitePaths,
    repo: Path,
    candidate: Candidate,
    lock: SuiteLock,
    *,
    created: date,
    release_date: date | None = None,
) -> tuple[SuiteLock, TaskMeta] | None:
    """Write one task; ``None`` when its hidden files are not UTF-8 text.

    With the student model's ``release_date``, a commit made before it is flagged as
    contaminated (B10): the model may have seen it in training.
    """
    task_id = task_id_for(candidate)
    hidden: dict[str, str] = {}
    for rel in (*candidate.test_files, *candidate.test_data):
        content = _show(repo, candidate.sha, rel)
        if content is None:
            return None
        try:
            hidden[rel] = content.decode("utf-8")
        except UnicodeDecodeError:
            return None
    tests = " ".join(candidate.test_files)
    check_sh = f"#!/usr/bin/env bash\nset -u\ncd /work\n{PYTEST} {tests}\n"
    # The visible check runs the parent's versions of the same test files, where they exist.
    visible = [rel for rel in candidate.test_files if _show(repo, candidate.parent, rel)]
    visible_check = f"{PYTEST} {' '.join(visible)}" if visible else None
    subject = _PR_SUFFIX.sub("", candidate.subject)
    task_text = f"# {subject}\n\n{candidate.body}\n".rstrip() + "\n"

    digest = hashlib.sha256()
    for part in (task_text, check_sh, *(f"{k}\0{v}" for k, v in sorted(hidden.items()))):
        digest.update(part.encode("utf-8"))
    lock = add_task(lock, task_id, "history", f"sha256:{digest.hexdigest()}")
    split = next(t.split for t in lock.tasks if t.id == task_id)
    meta = TaskMeta(
        schema_version=TASK_SCHEMA_VERSION,
        id=task_id,
        source="history",
        type=task_type_for(candidate),
        split=split,
        repo=RepoRef(base_commit=candidate.parent),
        visible_check=visible_check,
        created=created,
        contaminated=release_date is not None and candidate.committed.date() < release_date,
    )
    SealedWriter(paths.sealed_dir).write_task(
        task_id,
        check_sh=check_sh,
        hidden_tests=hidden,
        target_commit=candidate.sha,
        replace=True,
    )
    public = paths.public_dir / task_id
    public.mkdir(parents=True, exist_ok=True)
    (public / "task.md").write_text(task_text, encoding="utf-8")
    (public / "meta.yaml").write_text(
        yaml.safe_dump(meta.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )
    return lock, meta
