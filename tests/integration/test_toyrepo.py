import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.fixtures.toyrepo.build import FIXES, build

pytestmark = pytest.mark.integration


# Inherited repository-location variables (set by git hooks) would override cwd.
_GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")
_GIT_ENV = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}

# pytest exit codes: 0 all passed, 1 some tests failed; anything else is a broken run.
PASSED, TESTS_FAILED = 0, 1


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, env=_GIT_ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


def run_pytest(repo: Path, test: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:cov",
            test,
        ],
        cwd=repo,
        # Same-size edits within one second would otherwise reuse stale bytecode.
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )


def assert_outcome(result: subprocess.CompletedProcess[str], expected: int, what: str) -> None:
    assert result.returncode == expected, (
        f"{what}: pytest exited {result.returncode}, expected {expected}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


@pytest.fixture(scope="module")
def toyrepo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build(tmp_path_factory.mktemp("toy") / "repo")


def test_history_has_one_commit_per_fix(toyrepo: Path) -> None:
    subjects = git(toyrepo, "log", "--reverse", "--format=%s").splitlines()
    assert subjects == ["feat: add toykit helpers", *(fix[0] for fix in FIXES)]
    assert len(subjects) == 9


def test_build_is_deterministic(toyrepo: Path, tmp_path: Path) -> None:
    again = build(tmp_path / "again")
    assert git(again, "rev-parse", "HEAD") == git(toyrepo, "rev-parse", "HEAD")


def test_each_fix_test_fails_on_parent_and_passes_on_commit(toyrepo: Path) -> None:
    shas = git(toyrepo, "log", "--reverse", "--format=%H").splitlines()
    try:
        for sha, fix in zip(shas[1:], FIXES, strict=True):
            test = f"tests/{fix[4]}"
            git(toyrepo, "checkout", "--quiet", sha)
            assert_outcome(run_pytest(toyrepo, test), PASSED, f"{test} on {fix[0]}")
            git(toyrepo, "checkout", "--quiet", f"{sha}^", "--", "src")
            assert_outcome(run_pytest(toyrepo, test), TESTS_FAILED, f"{test} before {fix[0]}")
            git(toyrepo, "checkout", "--quiet", sha, "--", "src")
    finally:
        git(toyrepo, "checkout", "--quiet", "--force", "main")


def test_build_refuses_existing_destination(tmp_path: Path) -> None:
    with pytest.raises(FileExistsError):
        build(tmp_path)
