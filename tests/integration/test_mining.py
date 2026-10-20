import os
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.fixtures.toyrepo.build import FIXES, build

from saddlefit.tasks.mining import (
    Candidate,
    LocalRunner,
    check_candidate,
    is_test_path,
    iter_commits,
    mine,
    task_id_for,
    task_type_for,
)

pytestmark = pytest.mark.integration
RUNNER = LocalRunner(python=sys.executable)


@pytest.fixture(scope="module")
def toyrepo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build(tmp_path_factory.mktemp("mine") / "repo")


def git(repo: Path, *args: str) -> None:
    # isolated from the user's git config and hooks, like the toy repository builder
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(repo),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


def test_toy_history_yields_one_task_per_fix(toyrepo: Path) -> None:
    report = mine(toyrepo, RUNNER)
    assert report.summary() == {"scanned": 9, "viable": 8, "rejected": {"root_commit": 1}}
    subjects = {c.subject for c in report.viable}
    assert subjects == {fix[0] for fix in FIXES}
    first = next(c for c in report.viable if c.subject == FIXES[0][0])
    assert first.test_files == ("tests/test_parse_iso.py",)
    assert first.source_files == ("src/toykit/dates.py",)


def test_limit_and_since(toyrepo: Path) -> None:
    assert len(iter_commits(toyrepo, limit=3)) == 3
    assert iter_commits(toyrepo, since=date(2030, 1, 1)) == []


class Scripted:
    def __init__(self, on_commit: int, on_parent: int) -> None:
        self.codes = [on_commit, on_parent]

    def run_tests(self, snapshot: Path, test_files: Sequence[str]) -> int:
        return self.codes.pop(0)


@pytest.mark.parametrize(
    ("on_commit", "on_parent", "verdict"),
    [
        (0, 1, None),
        (0, 2, None),
        (0, 0, "passes_on_parent"),
        (1, 1, "fails_on_commit"),
        (0, 5, "passes_on_parent"),
    ],
)
def test_verdicts(toyrepo: Path, on_commit: int, on_parent: int, verdict: str | None) -> None:
    candidate = next(c for c, _ in iter_commits(toyrepo) if c is not None)
    assert check_candidate(toyrepo, candidate, Scripted(on_commit, on_parent)) == verdict


class Broken:
    def run_tests(self, snapshot: Path, test_files: Sequence[str]) -> int:
        raise subprocess.TimeoutExpired("pytest", 1)


def test_runner_errors_are_counted(toyrepo: Path) -> None:
    candidate = next(c for c, _ in iter_commits(toyrepo) if c is not None)
    assert check_candidate(toyrepo, candidate, Broken()) == "runner_error"


def test_rejections_for_test_only_source_only_and_deleted_tests(tmp_path: Path) -> None:
    repo = build(tmp_path / "repo")
    (repo / "tests" / "test_extra.py").write_text("def test_x() -> None:\n    pass\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "test only")
    (repo / "src" / "toykit" / "extra.py").write_text("X = 1\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "source only")
    (repo / "tests" / "test_extra.py").unlink()
    (repo / "src" / "toykit" / "extra.py").write_text("X = 2\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "delete test")
    reasons = [reason for _, reason in iter_commits(repo, limit=3)]
    assert reasons == ["test_deleted", "no_test_change", "no_source_change"]


@pytest.mark.parametrize(
    ("path", "is_test"),
    [
        ("tests/test_a.py", True),
        ("pkg/test_b.py", True),
        ("pkg/b_test.py", True),
        ("tests/data.json", False),
        ("src/testing.py", False),
    ],
)
def test_test_path_detection(path: str, is_test: bool) -> None:
    assert is_test_path(path) is is_test


def candidate(subject: str) -> Candidate:
    return Candidate(
        sha="abcdef1234567",
        parent="p",
        committed=datetime.now(UTC),
        subject=subject,
        body="",
        test_files=(),
        source_files=(),
    )


@pytest.mark.parametrize(
    ("subject", "task_id", "task_type"),
    [
        (
            "fix: parse_iso reads month before day",
            "parse-iso-reads-month-before-day-abcdef1",
            "bugfix",
        ),
        ("feat(cli)!: Add JSON output", "add-json-output-abcdef1", "feature"),
        ("Refactor everything!!!", "refactor-everything-abcdef1", "change"),
        ("!!!", "change-abcdef1", "change"),
        ("feat(eval): add leak guard (#65)", "add-leak-guard-abcdef1", "feature"),
    ],
)
def test_ids_and_types(subject: str, task_id: str, task_type: str) -> None:
    assert task_id_for(candidate(subject)) == task_id
    assert task_type_for(candidate(subject)) == task_type
