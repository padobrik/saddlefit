"""Mining tasks from git history: commits whose tests fail before them and pass after (A11).

A candidate is a non-merge commit that changes both test files and other Python files. It is
viable when the commit's versions of its test files fail on the parent's sources and pass
on the commit's. Running the tests is delegated to a ``Runner``, so the same check works on
the host for trusted repositories or inside an evaluation container.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Final, Literal, Protocol

from saddlefit.eval.tamper import matches_any

DEFAULT_TEST_PATTERNS: tuple[str, ...] = ("tests/**", "**/test_*.py", "**/*_test.py")
# pytest: 0 all passed, 1 tests failed, 2 interrupted (e.g. an import error in a new test)
_FAILED_ON_PARENT = frozenset({1, 2})
_FIELD = "\x1f"
_RECORD = "\x1e"

# Saddlefit's own state, sealed tasks included, never enters a snapshot or a task (B3).
STATE_DIR: Final = ".saddlefit"

Rejection = Literal[
    "root_commit",
    "touches_state",
    "no_test_change",
    "no_source_change",
    "test_deleted",
    "passes_on_parent",
    "fails_on_commit",
    "snapshot_error",
    "runner_error",
]


class Runner(Protocol):
    """Runs ``pytest`` on the given test files inside a source snapshot."""

    def run_tests(self, snapshot: Path, test_files: Sequence[str]) -> int: ...


@dataclass(frozen=True)
class Candidate:
    sha: str
    parent: str
    committed: datetime
    subject: str
    body: str
    test_files: tuple[str, ...]
    source_files: tuple[str, ...]
    # non-Python files under the test patterns (golden files, fixtures, snapshots)
    test_data: tuple[str, ...] = ()


@dataclass
class MiningReport:
    """How many candidates became tasks, and why the rest did not."""

    scanned: int = 0
    viable: list[Candidate] = field(default_factory=list)
    rejected: Counter[str] = field(default_factory=Counter)

    def summary(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "viable": len(self.viable),
            "rejected": dict(sorted(self.rejected.items())),
        }


def _git_env(**extra: str) -> dict[str, str]:
    """The environment for git: no inherited GIT_* variables, so ``cwd`` picks the repo."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return {**env, **extra}


def _git_bytes(repo: Path, *args: str, env: dict[str, str] | None = None) -> bytes:
    # core.quotePath=false: non-ASCII paths come out as UTF-8, not C-quoted
    result = subprocess.run(  # noqa: S603 - fixed git argv
        ["git", "-c", "core.quotePath=false", *args],  # noqa: S607
        cwd=repo,
        env=env or _git_env(),
        capture_output=True,
        check=True,
    )
    return result.stdout


def _git(repo: Path, *args: str) -> str:
    return _git_bytes(repo, *args).decode("utf-8", errors="surrogateescape")


def is_test_path(path: str, patterns: Iterable[str] = DEFAULT_TEST_PATTERNS) -> bool:
    return path.endswith(".py") and matches_any(path, tuple(patterns))


@dataclass
class _ChangedFiles:
    tests: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    data: list[str] = field(default_factory=list)
    deleted_test: bool = False
    touches_state: bool = False


def _classify(changes: str) -> _ChangedFiles:
    """Sort one commit's ``--name-status`` lines into tests, test data and sources."""
    files = _ChangedFiles()
    for line in changes.strip().splitlines():
        status, _, path = line.partition("\t")
        if path == STATE_DIR or path.startswith(STATE_DIR + "/"):
            files.touches_state = True
        elif is_test_path(path):
            files.deleted_test = files.deleted_test or status.startswith("D")
            files.tests.append(path)
        elif matches_any(path, DEFAULT_TEST_PATTERNS):
            files.deleted_test = files.deleted_test or status.startswith("D")
            files.data.append(path)
        elif path.endswith(".py"):
            files.sources.append(path)
    return files


def _rejection(files: _ChangedFiles, *, has_parent: bool) -> Rejection | None:
    if not has_parent:
        return "root_commit"
    if files.touches_state:
        return "touches_state"
    if not files.tests:
        return "no_test_change"
    if not files.sources:
        return "no_source_change"
    if files.deleted_test:
        return "test_deleted"
    return None


def iter_commits(
    repo: Path, *, since: date | None = None, limit: int | None = None
) -> list[tuple[Candidate | None, str | None]]:
    """Every non-merge commit, newest first, classified as a candidate or a rejection."""
    args = [
        "log",
        "--no-merges",
        "--no-renames",
        "--name-status",
        f"--format={_RECORD}%H{_FIELD}%P{_FIELD}%ct{_FIELD}%s{_FIELD}%b{_FIELD}",
    ]
    if since is not None:
        args.append(f"--since={since.isoformat()}")
    if limit is not None:
        args.append(f"--max-count={limit}")
    out: list[tuple[Candidate | None, str | None]] = []
    for record in _git(repo, *args).split(_RECORD)[1:]:
        sha, parents, stamp, subject, body, changes = record.split(_FIELD, 5)
        files = _classify(changes)
        reason = _rejection(files, has_parent=bool(parents.strip()))
        candidate = None
        if reason is None:
            candidate = Candidate(
                sha=sha,
                parent=parents.split()[0],
                committed=datetime.fromtimestamp(int(stamp), UTC),
                subject=subject.strip(),
                body=body.strip(),
                test_files=tuple(sorted(files.tests)),
                source_files=tuple(sorted(files.sources)),
                test_data=tuple(sorted(files.data)),
            )
        out.append((candidate, reason))
    return out


def _export(repo: Path, ref: str, dest: Path) -> None:
    """Materialize ``ref`` exactly as committed (``git archive`` would honour export-ignore)."""
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="saddlefit-index-") as tmp:
        env = _git_env(GIT_INDEX_FILE=str(Path(tmp) / "index"))
        _git_bytes(repo, "read-tree", ref, env=env)
        # a repository that committed its state by mistake must not leak it into snapshots
        _git_bytes(repo, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", STATE_DIR, env=env)
        _git_bytes(repo, "checkout-index", "--all", f"--prefix={dest}{os.sep}", env=env)


def _overlay(repo: Path, ref: str, files: Sequence[str], dest: Path) -> None:
    for rel in files:
        content = _git_bytes(repo, "show", f"{ref}:{rel}")
        target = dest / PurePosixPath(rel)
        # never write through a symlink: not the file itself, nor a directory above it
        for ancestor in target.relative_to(dest).parents:
            if ancestor != Path(".") and (dest / ancestor).is_symlink():
                raise SnapshotError(f"{rel} lies under a symlinked directory")
        if target.is_symlink():
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        if _is_symlink_entry(repo, ref, rel):
            # recreate the link (its blob is the target path), but only inside the snapshot
            link_target = content.decode("utf-8", errors="surrogateescape")
            try:
                resolved = (target.parent / link_target).resolve()
            except (RuntimeError, OSError) as exc:  # a symlink loop (RuntimeError on 3.12)
                raise SnapshotError(f"{rel} cannot be resolved: {exc}") from exc
            if PurePosixPath(link_target).is_absolute() or not resolved.is_relative_to(
                dest.resolve()
            ):
                raise SnapshotError(f"{rel} links outside the snapshot")
            target.symlink_to(link_target)
        else:
            target.write_bytes(content)


def _is_symlink_entry(repo: Path, ref: str, rel: str) -> bool:
    """Whether ``rel`` is committed as a symlink (git mode 120000) at ``ref``."""
    entry = _git_bytes(repo, "ls-tree", ref, "--", rel).decode("utf-8", errors="replace")
    return entry.startswith("120000 ")


class SnapshotError(Exception):
    """A snapshot of the parent or the commit could not be built safely."""


def check_candidate(repo: Path, candidate: Candidate, runner: Runner) -> Rejection | None:
    """``None`` when the commit's tests fail on the parent and pass on the commit."""
    with tempfile.TemporaryDirectory(prefix="saddlefit-mine-") as tmp:
        before = Path(tmp) / "parent"
        after = Path(tmp) / "commit"
        try:
            _export(repo, candidate.parent, before)
            # the commit's tests need the commit's test data, not the parent's
            _overlay(repo, candidate.sha, candidate.test_files + candidate.test_data, before)
            _export(repo, candidate.sha, after)
        except (SnapshotError, OSError, subprocess.SubprocessError):
            return "snapshot_error"
        try:
            on_commit = runner.run_tests(after, candidate.test_files)
            if on_commit != 0:
                return "fails_on_commit"
            on_parent = runner.run_tests(before, candidate.test_files)
        except (OSError, subprocess.SubprocessError):
            return "runner_error"
        return None if on_parent in _FAILED_ON_PARENT else "passes_on_parent"


def mine(
    repo: Path,
    runner: Runner,
    *,
    since: date | None = None,
    limit: int | None = None,
) -> MiningReport:
    """Scan history and check every candidate; ``limit`` caps the commits scanned."""
    report = MiningReport()
    for candidate, reason in iter_commits(repo, since=since, limit=limit):
        report.scanned += 1
        if candidate is None:
            report.rejected[reason or "unknown"] += 1
            continue
        verdict = check_candidate(repo, candidate, runner)
        if verdict is None:
            report.viable.append(candidate)
        else:
            report.rejected[verdict] += 1
    return report


_SLUG = re.compile(r"[^a-z0-9]+")


def task_id_for(candidate: Candidate) -> str:
    """A readable, unique ID: the subject as a slug plus the full SHA."""
    subject = re.sub(r"^\w+(\([^)]*\))?!?:\s*", "", candidate.subject.lower())
    subject = re.sub(r"\s*\(#\d+\)$", "", subject)  # squash-merge PR reference
    slug = _SLUG.sub("-", subject).strip("-")[:48].strip("-") or "change"
    # a short SHA can repeat across commits that also share a subject
    return f"{slug}-{candidate.sha}"


def task_type_for(candidate: Candidate) -> str:
    """bugfix, feature or change, from a Conventional Commit prefix when there is one."""
    prefix = candidate.subject.split(":", 1)[0].split("(", 1)[0].strip().rstrip("!").lower()
    return {"fix": "bugfix", "feat": "feature"}.get(prefix, "change")


@dataclass(frozen=True)
class LocalRunner:
    """Runs pytest on the host. Only for repositories you trust: their code executes here."""

    python: str = "python"
    timeout_s: float = 300

    def run_tests(self, snapshot: Path, test_files: Sequence[str]) -> int:
        # src-layout projects: import the snapshot's package, not an installed copy
        paths = [str(snapshot / "src")] if (snapshot / "src").is_dir() else []
        pythonpath = os.pathsep.join([*paths, os.environ.get("PYTHONPATH", "")]).strip(os.pathsep)
        result = subprocess.run(  # noqa: S603 - argv built from a fixed command and file names
            [self.python, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", *test_files],
            cwd=snapshot,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": pythonpath},
            capture_output=True,
            timeout=self.timeout_s,
            check=False,
        )
        return result.returncode
