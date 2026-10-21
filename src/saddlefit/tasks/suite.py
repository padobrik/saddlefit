"""The task suite on disk: ``.saddlefit/tasks/`` with public, sealed and ``suite.lock`` (B3).

``materialize`` turns a viable mined commit into a task: the public part (``task.md`` and
``meta.yaml``) for the student, and the sealed part (the commit's tests, ``check.sh`` and a
canary) for the evaluator only. The base snapshot is the commit's parent.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from saddlefit.config import SplitsConfig
from saddlefit.errors import UserError
from saddlefit.tasks.mining import Candidate, _git_bytes, task_id_for, task_type_for
from saddlefit.tasks.models import TASK_SCHEMA_VERSION, RepoRef, SuiteLock, TaskMeta
from saddlefit.tasks.sealing import SealedWriter
from saddlefit.tasks.splits import add_task, check_fractions

PYTEST = "PYTHONPATH=src:. python -B -m pytest -q -p no:cacheprovider"
_PR_SUFFIX = re.compile(r"\s*\(#\d+\)$")
_CONVENTIONAL = re.compile(r"^\w+(\([^)]*\))?!?:\s*")
# Sections of a pull-request template that say what was done, not what to do.
_TEMPLATE_SECTION = re.compile(
    r"^#{1,6}\s*(linked issues?|checklist|evaluation-integrity impact|test plan)\b", re.I
)
_DROPPED_LINE = re.compile(
    r"^\s*(- \[[ xX]\]|(closes|fixes|resolves|part of)\s+#\d+|signed-off-by:|co-authored-by:)",
    re.I,
)
INSTRUCTION = (
    "Make this change in the repository. It is not in the repository yet: implement it so the "
    "project's tests for it pass."
)


def task_text_for(candidate: Candidate) -> str:
    """What the student reads: an instruction built from the commit, not a report of it.

    A squash commit's body is often the pull request's description, which says the work is
    done and ends in a checklist; those parts are left out.
    """
    title = _CONVENTIONAL.sub("", _PR_SUFFIX.sub("", candidate.subject.strip()))
    title = title[:1].upper() + title[1:] if title else "Make the change"
    kept: list[str] = []
    for line in candidate.body.splitlines():
        if _TEMPLATE_SECTION.match(line):
            break
        if not _DROPPED_LINE.match(line):
            kept.append(line)
    body = re.sub(r"^#{1,6}\s*summary\s*$", "", "\n".join(kept), flags=re.I | re.M).strip()
    parts = [f"# {title}", INSTRUCTION, body]
    return "\n\n".join(part for part in parts if part) + "\n"


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
    """Read ``suite.lock``; only ``init`` creates one, so a missing lock is an error.

    A fresh salt would silently move every task to a new split (B10).
    """
    if not paths.lock.exists():
        raise UserError(f"{paths.lock} is missing; splits cannot be recovered without it")
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


def _visible_check(repo: Path, candidate: Candidate) -> str:
    """The parent's versions of the same test files, else the tests beside them, else all.

    A feature usually adds new test files, which the student must not see; the existing tests
    in the same directories still tell it whether it broke something. Every history task gets
    a check run like the hidden one, so the finish gate never falls back to a bare command that
    cannot import a src-layout project.
    """
    existing = [rel for rel in candidate.test_files if _show(repo, candidate.parent, rel)]
    if not existing:
        dirs = sorted({posixpath.dirname(rel) for rel in candidate.test_files} - {""})
        existing = [d for d in dirs if _show(repo, candidate.parent, d)]
    return " ".join([PYTEST, *(shlex.quote(rel) for rel in existing)])


def materialize(
    paths: SuitePaths,
    repo: Path,
    candidate: Candidate,
    lock: SuiteLock,
    *,
    created: date,
) -> tuple[SuiteLock, TaskMeta] | None:
    """Write one task; ``None`` when its hidden files are not UTF-8 text."""
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
    tests = " ".join(shlex.quote(rel) for rel in candidate.test_files)
    check_sh = f"#!/usr/bin/env bash\nset -u\ncd /work\n{PYTEST} {tests}\n"
    visible_check = _visible_check(repo, candidate)
    task_text = task_text_for(candidate)

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
        committed=candidate.committed.date(),
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
