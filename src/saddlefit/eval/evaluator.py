"""The evaluator: a fresh container, the student's diff, the hidden tests, ``check.sh`` (B10).

Every evaluation starts from the base snapshot in a new container with no network. The
student's diff is applied inside it; then the hidden tests and ``check.sh`` are added and
run. A diff that touches protected paths or the hidden tests' paths is ``tampered`` and is
not run at all. Hidden-check output goes only to the sealed store; the verdict carries the
exit code and duration, never the output.
"""

from __future__ import annotations

import logging
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

log = logging.getLogger(__name__)

DIFF_FILE = ".saddlefit-student.diff"
# Files that steer pytest or the interpreter running it; a diff may never touch them, whatever
# evaluation.protected_paths says, or it could change how the hidden tests run or score.
PYTEST_CONTROL_PATHS: tuple[str, ...] = (
    "conftest.py",
    "**/conftest.py",
    "sitecustomize.py",
    "**/sitecustomize.py",
    "usercustomize.py",
    "**/usercustomize.py",
    "*.pth",
    "**/*.pth",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "pyproject.toml",
)
CHECK_FILE = ".saddlefit-check.sh"


def protected_patterns(evaluation: EvaluationConfig) -> tuple[str, ...]:
    """Every pattern a diff may not touch: the configured paths plus the pytest control files."""
    return (*evaluation.protected_paths, *PYTEST_CONTROL_PATHS)


APPLY_TIMEOUT_S = 60
# a student's diff is untrusted input to a host-side git process: bound its size and time
MAX_DIFF_BYTES = 1_000_000
PARSE_TIMEOUT_S = 30
# room for container start-up and the diff on top of the check's own timeout
RUN_MARGIN_S = 120


@dataclass(frozen=True)
class Verdict:
    """The evaluator's verdict on one run; deliberately without the check's output."""

    passed: bool
    exit_code: int | None
    duration_s: float
    timed_out: bool = False
    # only whether it was tampered with: which paths would reveal where hidden tests live
    tampered: bool = False
    diff_applied: bool = True
    # only whether the environment failed; the message goes to the log, not to the verdict
    env_error: bool = False


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

    @classmethod
    def for_suite(
        cls,
        sealed_root: Path,
        docker_client: Any,
        image: str,
        limits: SandboxConfig,
        evaluation: EvaluationConfig,
    ) -> Evaluator:
        """An evaluator over ``tasks/sealed``; callers outside eval never touch the store."""
        return cls(SealedStore(sealed_root), docker_client, image, limits, evaluation)

    def evaluate(self, task_id: str, base_snapshot: Path, diff: str, run_id: str) -> Verdict:
        """Judge ``diff`` against task ``task_id`` starting from ``base_snapshot``."""
        # encode once, strictly, before anything parses the diff; the same bytes then go to
        # the host-side parse and into the container
        try:
            raw = diff.encode("utf-8")
        except UnicodeEncodeError:
            return Verdict(passed=False, exit_code=None, duration_s=0.0, diff_applied=False)
        if len(raw) > MAX_DIFF_BYTES:
            return Verdict(passed=False, exit_code=None, duration_s=0.0, diff_applied=False)
        touched = diff_paths(diff)
        if diff.strip():
            applied = _applied_paths(raw)
            if applied is None:
                return Verdict(passed=False, exit_code=None, duration_s=0.0, diff_applied=False)
            # git apply also accepts diffs without a "diff --git" header; judge what it applies
            touched |= applied
        hidden = self._store.hidden_test_paths(task_id)
        protected = protected_patterns(self._evaluation)
        tampered = _tampered(touched, protected, hidden)
        if tampered:
            return Verdict(passed=False, exit_code=None, duration_s=0.0, tampered=True)
        with tempfile.TemporaryDirectory(prefix="saddlefit-eval-") as tmp:
            work = Path(tmp) / "work"
            shutil.copytree(base_snapshot, work, symlinks=True)
            try:
                return self._run(task_id, work, raw, hidden, run_id)
            except SandboxError as exc:
                log.warning("evaluation of %s hit an environment error: %s", task_id, exc)
                return Verdict(passed=False, exit_code=None, duration_s=0.0, env_error=True)

    def _run(
        self, task_id: str, work: Path, diff: bytes, hidden: list[str], run_id: str
    ) -> Verdict:
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
                staged = _stage_diff(work, diff)
                applied = box.exec(f"git apply --whitespace=nowarn {DIFF_FILE}", APPLY_TIMEOUT_S)
                staged.unlink(missing_ok=True)
                if applied.exit_code != 0:
                    return Verdict(passed=False, exit_code=None, duration_s=0.0, diff_applied=False)
            try:
                self._install_sealed(task_id, work, hidden)
            except PlantedSymlinkError:
                return Verdict(passed=False, exit_code=None, duration_s=0.0, tampered=True)
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


def _applied_paths(diff: bytes) -> set[str] | None:
    """Every path ``git apply`` would touch, from its own parser; ``None`` if it cannot parse."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    with tempfile.TemporaryDirectory(prefix="saddlefit-numstat-") as tmp:
        try:
            result = subprocess.run(
                ["git", "apply", "--numstat", "-z", "-"],  # noqa: S607
                cwd=tmp,  # outside any repository, so nothing is resolved against one
                env=env,
                input=diff,
                capture_output=True,
                timeout=PARSE_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None
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


def _stage_diff(work: Path, diff: bytes) -> Path:
    """Write the diff for ``git apply``; this host-side write never follows a symlink."""
    path = _inside(work, DIFF_FILE)
    path.write_bytes(diff)
    return path


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
