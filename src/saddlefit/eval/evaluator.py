"""The evaluator: a fresh container, the student's diff, the hidden tests, ``check.sh`` (B10).

Every evaluation starts from the base snapshot in a new container with no network. The
student's diff is applied inside it; then the hidden tests and ``check.sh`` are added and
run. A diff that touches protected paths or the hidden tests' paths is ``tampered`` and is
not run at all. Hidden-check output goes only to the sealed store; the verdict carries the
exit code and duration, never the output.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from saddlefit.config import EvaluationConfig, SandboxConfig
from saddlefit.eval.sealed import SealedStore
from saddlefit.eval.tamper import diff_paths, tampered_paths
from saddlefit.sandbox.container import Sandbox, SandboxError

DIFF_FILE = ".saddlefit-student.diff"
CHECK_FILE = ".saddlefit-check.sh"
APPLY_TIMEOUT_S = 60
# room for container start-up and the diff on top of the check's own timeout
RUN_MARGIN_S = 120


@dataclass(frozen=True)
class Verdict:
    """The evaluator's verdict on one run; deliberately without the check's output."""

    passed: bool
    exit_code: int | None
    duration_s: float
    timed_out: bool = False
    tampered: tuple[str, ...] = ()
    diff_applied: bool = True
    env_error: str | None = None


class Evaluator:
    """Judges a student's diff in a fresh no-network container (B10).

    Hidden-check output goes to the sealed store only; callers get a ``Verdict``.
    """

    def __init__(
        self,
        store: SealedStore,
        docker_client: Any,
        image: str,
        limits: SandboxConfig,
        evaluation: EvaluationConfig,
    ) -> None:
        self._store = store
        self._client = docker_client
        self._image = image
        self._limits = limits
        self._evaluation = evaluation

    def evaluate(self, task_id: str, base_snapshot: Path, diff: str, run_id: str) -> Verdict:
        """Judge ``diff`` against task ``task_id`` starting from ``base_snapshot``."""
        touched = diff_paths(diff)
        if diff.strip():
            applied = _applied_paths(diff)
            if applied is None:
                return Verdict(passed=False, exit_code=None, duration_s=0.0, diff_applied=False)
            # git apply also accepts diffs without a "diff --git" header; judge what it applies
            touched |= applied
        hidden = self._store.hidden_test_paths(task_id)
        tampered = _tampered(touched, self._evaluation.protected_paths, hidden)
        if tampered:
            return Verdict(passed=False, exit_code=None, duration_s=0.0, tampered=tuple(tampered))
        with tempfile.TemporaryDirectory(prefix="saddlefit-eval-") as tmp:
            work = Path(tmp) / "work"
            shutil.copytree(base_snapshot, work, symlinks=True)
            try:
                return self._run(task_id, work, diff, hidden, run_id)
            except SandboxError as exc:
                return Verdict(passed=False, exit_code=None, duration_s=0.0, env_error=str(exc))

    def _run(self, task_id: str, work: Path, diff: str, hidden: list[str], run_id: str) -> Verdict:
        timeout = self._evaluation.timeout_seconds
        with Sandbox(
            self._client,
            self._image,
            work,
            self._limits,
            run_timeout_s=timeout + RUN_MARGIN_S,
            labels={"saddlefit.role": "evaluation", "saddlefit.task": task_id},
        ) as box:
            if diff.strip():
                (work / DIFF_FILE).write_text(diff, encoding="utf-8")
                applied = box.exec(f"git apply --whitespace=nowarn {DIFF_FILE}", APPLY_TIMEOUT_S)
                (work / DIFF_FILE).unlink(missing_ok=True)
                if applied.exit_code != 0:
                    return Verdict(passed=False, exit_code=None, duration_s=0.0, diff_applied=False)
            try:
                self._install_sealed(task_id, work, hidden)
            except PlantedSymlinkError as exc:
                return Verdict(passed=False, exit_code=None, duration_s=0.0, tampered=(str(exc),))
            result = box.exec(f"bash {CHECK_FILE}", timeout)
        self._store.record_check_output(task_id, run_id, result.output)
        return Verdict(
            passed=result.exit_code == 0 and not result.timed_out,
            exit_code=result.exit_code,
            duration_s=result.duration_s,
            timed_out=result.timed_out,
        )

    def _install_sealed(self, task_id: str, work: Path, hidden: list[str]) -> None:
        """Copy the hidden tests and check.sh in, after the student's diff is applied.

        The copy happens on the host, into a tree the student's diff has changed, so a
        symlink planted by the diff must never be followed.
        """
        source = self._store.hidden_tests_dir(task_id)
        for rel in hidden:
            target = _inside(work, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / PurePosixPath(rel), target)
        shutil.copyfile(self._store.check_script(task_id), _inside(work, CHECK_FILE))


def _applied_paths(diff: str) -> set[str] | None:
    """Every path ``git apply`` would touch, from its own parser; ``None`` if it cannot parse."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    with tempfile.TemporaryDirectory(prefix="saddlefit-numstat-") as tmp:
        result = subprocess.run(
            ["git", "apply", "--numstat", "-z", "-"],  # noqa: S607
            cwd=tmp,  # outside any repository, so nothing is resolved against one
            env=env,
            input=diff.encode("utf-8", errors="surrogateescape"),
            capture_output=True,
            check=False,
        )
    if result.returncode != 0:
        return None
    paths: set[str] = set()
    fields = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    i = 0
    while i < len(fields):
        added_deleted_path = fields[i].split("\t", 2)
        if len(added_deleted_path) == 3 and added_deleted_path[2]:
            paths.add(added_deleted_path[2])
            i += 1
        elif len(added_deleted_path) == 3:
            # a rename or copy: the old and new names follow as separate fields
            paths.update(name for name in fields[i + 1 : i + 3] if name)
            i += 3
        else:
            i += 1
    return paths


def _tampered(touched: set[str], protected: tuple[str, ...], hidden: list[str]) -> list[str]:
    """Tampered paths, including any that stand in the way of a file the evaluator writes.

    The evaluator copies the hidden tests and ``check.sh`` in after the diff is applied, so
    a diff may not create those paths, a file where one needs a directory, or a directory
    where one needs a file.
    """
    reserved = [*hidden, CHECK_FILE, DIFF_FILE]
    in_the_way = {
        path
        for path in touched
        for name in reserved
        if path.startswith(name + "/") or name.startswith(path + "/")
    }
    return sorted(set(tampered_paths(touched, protected, reserved)) | in_the_way)


class PlantedSymlinkError(Exception):
    """The student's diff put a symlink where sealed files are about to be copied."""


def _inside(work: Path, rel: str) -> Path:
    """``work/rel`` with no symlink on the way and no escape; a symlink at the end is removed."""
    target = work / PurePosixPath(rel)
    current = work
    for part in PurePosixPath(rel).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise PlantedSymlinkError(f"{current.relative_to(work)} is a symlink")
    if target.is_symlink():
        target.unlink()
    if not target.resolve().is_relative_to(work.resolve()):
        raise PlantedSymlinkError(f"{rel} resolves outside the workspace")
    return target
