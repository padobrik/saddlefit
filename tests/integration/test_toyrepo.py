import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.fixtures.toyrepo.build import FIXES, build

pytestmark = pytest.mark.integration


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def pytest_passes(repo: Path, test: str) -> bool:
    result = subprocess.run(
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
    return result.returncode == 0


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
            assert pytest_passes(toyrepo, test), f"{test} should pass on {fix[0]}"
            git(toyrepo, "checkout", "--quiet", f"{sha}^", "--", "src")
            assert not pytest_passes(toyrepo, test), f"{test} should fail before {fix[0]}"
            git(toyrepo, "checkout", "--quiet", sha, "--", "src")
    finally:
        git(toyrepo, "checkout", "--quiet", "--force", "main")


def test_build_refuses_existing_destination(tmp_path: Path) -> None:
    with pytest.raises(FileExistsError):
        build(tmp_path)
