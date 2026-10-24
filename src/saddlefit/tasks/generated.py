"""Files around a generated task: what the generator is shown, and where the task lands."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

import yaml

from saddlefit.eval.tamper import matches_any
from saddlefit.tasks.acceptance import check_script
from saddlefit.tasks.generator import GeneratedTask, RepoFile
from saddlefit.tasks.mining import _git_bytes
from saddlefit.tasks.models import TaskMeta
from saddlefit.tasks.suite import SuitePaths

MAX_FILE_CHARS: Final = 20_000
MAX_LISTED_PATHS: Final = 400
README_CHARS: Final = 3_000


def tracked_paths(repo: Path, ref: str) -> list[str]:
    out = _git_bytes(repo, "ls-tree", "-r", "-z", "--name-only", ref)
    return [p for p in out.decode("utf-8").split("\0") if p]


def repo_files(
    repo: Path, ref: str, paths: Sequence[str], protected: Sequence[str]
) -> list[RepoFile]:
    """Text files the generator may change: not protected, UTF-8, and not too long."""
    files = []
    for path in paths:
        if path.startswith(".") or matches_any(path, protected):
            continue
        try:
            text = _git_bytes(repo, "show", f"{ref}:{path}").decode("utf-8")
        except UnicodeDecodeError:
            continue
        if text.strip() and len(text) <= MAX_FILE_CHARS:
            files.append(RepoFile(path, text))
    return files


def repo_summary(repo: Path, ref: str, paths: Sequence[str]) -> str:
    """The file list and the start of the README."""
    listed = "\n".join(paths[:MAX_LISTED_PATHS])
    more = (
        f"\n... and {len(paths) - MAX_LISTED_PATHS} more" if len(paths) > MAX_LISTED_PATHS else ""
    )
    readme = next((p for p in paths if p.lower() in {"readme.md", "readme.rst", "readme"}), None)
    text = ""
    if readme:
        text = _git_bytes(repo, "show", f"{ref}:{readme}").decode("utf-8", "replace")
        text = f"\n\n## {readme}\n\n{text[:README_CHARS]}"
    return f"## Files\n\n{listed}{more}{text}"


def content_hash(task: GeneratedTask) -> str:
    """The suite-lock hash, built like a mined task's: text, check and hidden tests."""
    digest = hashlib.sha256()
    parts = (task.task_md, check_script(list(task.hidden_tests)))
    for part in (*parts, *(f"{k}\0{v}" for k, v in sorted(task.hidden_tests.items()))):
        digest.update(part.encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def publish(paths: SuitePaths, meta: TaskMeta, task: GeneratedTask) -> None:
    """Write the public part: ``task.md`` and ``meta.yaml``."""
    public = paths.public_dir / meta.id
    public.mkdir(parents=True, exist_ok=True)
    (public / "task.md").write_text(task.task_md, encoding="utf-8")
    (public / "meta.yaml").write_text(
        yaml.safe_dump(meta.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )


def _writable_and_retry(func: Callable[[str], object], target: str, exc: BaseException) -> None:
    if not isinstance(exc, PermissionError) or os.path.islink(target):
        raise exc
    os.chmod(target, stat.S_IWRITE)
    func(target)


def discard(paths: SuitePaths, task_id: str) -> None:
    """Remove both parts of a task that is not kept."""
    for root in (paths.public_dir, paths.sealed_dir):
        target = root / task_id
        if target.exists():
            shutil.rmtree(target, onexc=_writable_and_retry)
