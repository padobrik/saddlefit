import os
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.fixtures.toyrepo.build import FIXES, build

from saddlefit.tasks import mining
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
# Windows needs special rights to create symlinks, and git checks them out as plain files.
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="symlinks need POSIX semantics")
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


def candidate(subject: str, sha: str = "abcdef1234567") -> Candidate:
    return Candidate(
        sha=sha,
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
            "parse-iso-reads-month-before-day-abcdef1234567",
            "bugfix",
        ),
        ("feat(cli)!: Add JSON output", "add-json-output-abcdef1234567", "feature"),
        ("Refactor everything!!!", "refactor-everything-abcdef1234567", "change"),
        ("!!!", "change-abcdef1234567", "change"),
        ("feat(eval): add leak guard (#65)", "add-leak-guard-abcdef1234567", "feature"),
        ("fix!: drop py3.8", "drop-py3-8-abcdef1234567", "bugfix"),
        ("feat!: new api", "new-api-abcdef1234567", "feature"),
    ],
)
def test_ids_and_types(subject: str, task_id: str, task_type: str) -> None:
    assert task_id_for(candidate(subject)) == task_id
    assert task_type_for(candidate(subject)) == task_type


def test_commits_sharing_a_subject_and_short_sha_get_distinct_ids() -> None:
    first = candidate("fix: same subject", sha="abcdef1" + "0" * 33)
    second = candidate("fix: same subject", sha="abcdef1" + "f" * 33)
    assert task_id_for(first) != task_id_for(second)


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


@POSIX_ONLY
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


def test_changed_test_data_is_copied_into_the_parent(tmp_path: Path) -> None:
    repo = build(tmp_path / "repo")
    data = repo / "tests" / "data" / "expected.json"
    data.parent.mkdir(parents=True)
    data.write_text('{"answer": 1}\n', encoding="utf-8")
    (repo / "tests" / "test_golden.py").write_text(
        "import json, pathlib\n\n\ndef test_golden() -> None:\n"
        "    raw = (pathlib.Path(__file__).parent / 'data' / 'expected.json').read_text()\n"
        "    assert json.loads(raw)['answer'] == 1\n",
        encoding="utf-8",
    )
    commit_all(repo, "test: golden data")
    # the golden file and its test change together; the source change is cosmetic
    data.write_text('{"answer": 2}\n', encoding="utf-8")
    test = repo / "tests" / "test_golden.py"
    test.write_text(test.read_text(encoding="utf-8").replace("== 1", "== 2"), encoding="utf-8")
    src = repo / "src" / "toykit" / "numbers.py"
    src.write_text(src.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    commit_all(repo, "chore: update golden data")
    candidate = next(c for c, _ in iter_commits(repo, limit=1) if c is not None)
    assert candidate.test_data == ("tests/data/expected.json",)
    assert candidate.test_files == ("tests/test_golden.py",)
    # the parent's source passes the new test once the new data is there too
    assert check_candidate(repo, candidate, RUNNER) == "passes_on_parent"


def test_deleted_test_data_rejects_the_commit(tmp_path: Path) -> None:
    repo = build(tmp_path / "repo")
    data = repo / "tests" / "data.txt"
    data.write_text("x\n", encoding="utf-8")
    commit_all(repo, "test: add data")
    data.unlink()
    (repo / "tests" / "test_parse_iso.py").write_text("def test_x() -> None:\n    pass\n")
    (repo / "src" / "toykit" / "dates.py").write_text("X = 1\n")
    commit_all(repo, "fix: drop data")
    assert [r for _, r in iter_commits(repo, limit=1)] == ["test_deleted"]


class Snapshots:
    """Records each snapshot it is asked to test, for inspection after the check."""

    def __init__(self) -> None:
        self.seen: list[Path] = []

    def run_tests(self, snapshot: Path, test_files: Sequence[str]) -> int:
        link = snapshot / "tests" / "data" / "current.json"
        self.seen.append(link)
        assert link.is_symlink(), "test data symlink was flattened"
        assert link.resolve().read_text(encoding="utf-8") == "2\n"
        return 0 if len(self.seen) == 1 else 1


@POSIX_ONLY
def test_test_data_symlinks_stay_symlinks(tmp_path: Path) -> None:
    repo = build(tmp_path / "repo")
    data = repo / "tests" / "data"
    data.mkdir()
    (data / "v1.json").write_text("1\n", encoding="utf-8")
    (data / "v2.json").write_text("2\n", encoding="utf-8")
    (data / "current.json").symlink_to("v1.json")
    commit_all(repo, "test: data v1")
    (data / "current.json").unlink()
    (data / "current.json").symlink_to("v2.json")
    (repo / "tests" / "test_x.py").write_text("def test_x() -> None:\n    pass\n")
    (repo / "src" / "toykit" / "x.py").write_text("X = 1\n")
    commit_all(repo, "fix: point at v2")
    candidate = next(c for c, _ in iter_commits(repo, limit=1) if c is not None)
    assert "tests/data/current.json" in candidate.test_data
    assert check_candidate(repo, candidate, Snapshots()) is None


@POSIX_ONLY
def test_test_data_symlinks_leaving_the_snapshot_are_rejected(tmp_path: Path) -> None:
    repo = build(tmp_path / "repo")
    (repo / "tests" / "data.json").symlink_to("/etc/hosts")
    (repo / "tests" / "test_x.py").write_text("def test_x() -> None:\n    pass\n")
    (repo / "src" / "toykit" / "x.py").write_text("X = 1\n")
    commit_all(repo, "fix: link out")
    candidate = next(c for c, _ in iter_commits(repo, limit=1) if c is not None)
    assert check_candidate(repo, candidate, Scripted(0, 1)) == "snapshot_error"


@POSIX_ONLY
def test_a_symlink_loop_is_a_snapshot_error_not_a_crash(tmp_path: Path) -> None:
    repo = build(tmp_path / "repo")
    data = repo / "tests" / "data"
    data.mkdir()
    (data / "loop.json").symlink_to("loop.json")  # points at itself
    commit_all(repo, "test: a broken link")
    (data / "current.json").symlink_to("loop.json")
    (repo / "tests" / "test_x.py").write_text("def test_x() -> None:\n    pass\n")
    (repo / "src" / "toykit" / "x.py").write_text("X = 1\n")
    commit_all(repo, "fix: link to the loop")
    candidate = next(c for c, _ in iter_commits(repo, limit=1) if c is not None)
    verdict = check_candidate(repo, candidate, Scripted(0, 1))
    if sys.version_info < (3, 13):
        # 3.12 raises RuntimeError for a loop in resolve(); it must become snapshot_error
        assert verdict == "snapshot_error"
    else:
        # 3.13+ resolves a loop without raising; the link stays inside the snapshot
        assert verdict is None


def test_snapshots_never_contain_saddlefit_state(tmp_path: Path) -> None:
    repo = build(tmp_path / "repo")
    sealed = repo / ".saddlefit" / "tasks" / "sealed" / "fix-0001"
    sealed.mkdir(parents=True)
    (sealed / "check.sh").write_text("# hidden check\n", encoding="utf-8")
    commit_all(repo, "chore: commit state by mistake")
    dest = tmp_path / "snapshot"
    mining._export(repo, "HEAD", dest)
    assert (dest / "src" / "toykit").is_dir()
    assert not (dest / ".saddlefit").exists()


def test_commits_touching_saddlefit_state_are_rejected(tmp_path: Path) -> None:
    repo = build(tmp_path / "repo")
    state = repo / ".saddlefit" / "tasks" / "public"
    state.mkdir(parents=True)
    (state / "notes.py").write_text("x = 1\n", encoding="utf-8")
    add_fix(repo, "tests/test_double.py")
    (state / "notes.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "src" / "toykit" / "extra.py").write_text(
        "def double(x):\n    return 2 * x\n", encoding="utf-8"
    )
    (repo / "tests" / "test_double.py").write_text(
        "from toykit.extra import double\n\n\ndef test_double():\n    assert double(3) == 6\n",
        encoding="utf-8",
    )
    commit_all(repo, "fix: double again")
    newest, reason = iter_commits(repo, limit=1)[0]
    assert newest is None
    assert reason == "touches_state"


def test_export_drops_state_even_when_the_checkout_differs(tmp_path: Path) -> None:
    repo = build(tmp_path / "repo")
    state = repo / ".saddlefit" / "x"
    state.parent.mkdir()
    state.write_text("1\n", encoding="utf-8")
    commit_all(repo, "chore: state 1")
    state.write_text("2\n", encoding="utf-8")
    commit_all(repo, "chore: state 2")
    state.write_text("3\n", encoding="utf-8")
    dest = tmp_path / "snapshot"
    mining._export(repo, "HEAD~1", dest)
    assert (dest / "src" / "toykit").is_dir()
    assert not (dest / ".saddlefit").exists()


def test_local_runner_does_not_inherit_the_host_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "snapshot"
    (snapshot / "tests").mkdir(parents=True)
    (snapshot / "tests" / "test_env.py").write_text(
        "import os, sys\n\n\ndef test_env():\n"
        "    assert 'SADDLEFIT_TEST_SECRET' not in os.environ\n"
        "    assert os.environ.get('PWD') is None\n"
        "    assert not any('evil-path' in p for p in sys.path)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SADDLEFIT_TEST_SECRET", "x")
    monkeypatch.setenv("PWD", str(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "evil-path"))
    assert RUNNER.run_tests(snapshot, ["tests/test_env.py"]) == 0
