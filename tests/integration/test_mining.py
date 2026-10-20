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
        ("fix!: drop py3.8", "drop-py3-8-abcdef1", "bugfix"),
        ("feat!: new api", "new-api-abcdef1", "feature"),
    ],
)
def test_ids_and_types(subject: str, task_id: str, task_type: str) -> None:
    assert task_id_for(candidate(subject)) == task_id
    assert task_type_for(candidate(subject)) == task_type


def commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)


def add_fix(repo: Path, test_rel: str) -> None:
    """A commit that fixes clamp's bug again on a fresh function, tested in ``test_rel``."""
    src = repo / "src" / "toykit" / "extra.py"
    src.write_text("def double(x):\n    return x + x + 1\n", encoding="utf-8")
    commit_all(repo, "feat: add double")
    src.write_text("def double(x):\n    return x + x\n", encoding="utf-8")
    test = repo / test_rel
    test.parent.mkdir(parents=True, exist_ok=True)
    test.write_text(
        "from toykit.extra import double\n\n\ndef test_double():\n    assert double(2) == 4\n",
        encoding="utf-8",
    )
    commit_all(repo, "fix: double")


def test_non_ascii_test_paths(tmp_path: Path) -> None:
    repo = build(tmp_path / "repo")
    add_fix(repo, "tests/test_dátum.py")
    report = mine(repo, RUNNER, limit=1)
    assert report.summary()["viable"] == 1
    assert report.viable[0].test_files == ("tests/test_dátum.py",)


def test_export_ignore_does_not_hide_tests(tmp_path: Path) -> None:
    repo = build(tmp_path / "repo")
    (repo / ".gitattributes").write_text("tests/** export-ignore\n", encoding="utf-8")
    commit_all(repo, "chore: keep tests out of archives")
    add_fix(repo, "tests/test_double.py")
    assert mine(repo, RUNNER, limit=1).summary()["viable"] == 1


def test_inherited_git_dir_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = build(tmp_path / "repo")
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")  # an empty repository: none of repo's commits exist there
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    report = mine(repo, RUNNER, limit=2)
    assert report.summary()["viable"] == 2


def test_overlay_never_writes_through_a_symlink(tmp_path: Path) -> None:
    repo = build(tmp_path / "repo")
    source = repo / "src" / "toykit" / "numbers.py"
    original = source.read_text(encoding="utf-8")
    link = repo / "tests" / "test_link.py"
    link.symlink_to(Path("../src/toykit/numbers.py"))
    commit_all(repo, "chore: link")
    link.unlink()
    link.write_text("def test_x():\n    assert False\n", encoding="utf-8")
    source.write_text(original + "\n# touched\n", encoding="utf-8")
    commit_all(repo, "fix: replace link")
    candidate = next(c for c, _ in iter_commits(repo, limit=1) if c is not None)

    class Inspecting:
        def __init__(self) -> None:
            self.seen: list[str] = []

        def run_tests(self, snapshot: Path, test_files: Sequence[str]) -> int:
            self.seen.append((snapshot / "src/toykit/numbers.py").read_text(encoding="utf-8"))
            return 0 if len(self.seen) == 1 else 1

    runner = Inspecting()
    assert check_candidate(repo, candidate, runner) is None
    # the parent snapshot's source must be the parent's, not the overlaid test file
    assert runner.seen[1] == original


def test_snapshot_errors_are_a_rejection_not_a_crash(toyrepo: Path) -> None:
    good = next(c for c, _ in iter_commits(toyrepo) if c is not None)
    broken = Candidate(
        sha=good.sha,
        parent="0" * 40,
        committed=good.committed,
        subject=good.subject,
        body="",
        test_files=good.test_files,
        source_files=good.source_files,
    )
    assert check_candidate(toyrepo, broken, Scripted(0, 1)) == "snapshot_error"
