"""The evaluator against a real Docker daemon, including required tests 3 and 13."""

import shutil
import subprocess
from pathlib import Path

import pytest
from tests.fixtures.toyrepo.build import FIXES, build

from saddlefit.config import EvaluationConfig, SandboxConfig
from saddlefit.eval.evaluator import Evaluator
from saddlefit.eval.sealed import SealedStore
from saddlefit.tasks.sealing import SealedWriter

pytestmark = pytest.mark.docker

TASK = "parse-iso-month-day-0001"
RUN_ID = "01JB2Y8Q4V9N1K3D7M5R6T0W2X"
SENTINEL = "HIDDEN-CHECK-SENTINEL-7f3a"
FIX_DIFF = "\n".join(
    [
        "diff --git a/src/toykit/dates.py b/src/toykit/dates.py",
        "--- a/src/toykit/dates.py",
        "+++ b/src/toykit/dates.py",
        "@@ -5,5 +5,5 @@ from datetime import date",
        " ",  # an empty context line is a single space in a unified diff
        " def parse_iso(text: str) -> date:",
        '     """Parse YYYY-MM-DD."""',
        '-    year, day, month = text.split("-")',
        '+    year, month, day = text.split("-")',
        "     return date(int(year), int(month), int(day))",
        "",
    ]
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def setup(tmp_path: Path, docker_client, sandbox_image: str) -> tuple[Evaluator, Path, Path]:
    repo = build(tmp_path / "repo")
    first_fix = git(repo, "log", "--reverse", "--format=%H").splitlines()[1]
    git(repo, "checkout", "--quiet", f"{first_fix}^")
    base = tmp_path / "base"
    shutil.copytree(repo, base, ignore=shutil.ignore_patterns(".git"))
    sealed = tmp_path / "sealed"
    SealedWriter(sealed).write_task(
        TASK,
        check_sh=(
            "#!/usr/bin/env bash\ncd /work\n"
            f"echo {SENTINEL}\n"
            "PYTHONPATH=src python -B -m pytest -q -p no:cacheprovider tests/test_parse_iso.py\n"
        ),
        hidden_tests={"tests/test_parse_iso.py": FIXES[0][5]},
        target_commit=first_fix,
    )
    evaluator = Evaluator(
        SealedStore(sealed),
        docker_client,
        sandbox_image,
        SandboxConfig(memory="512m"),
        EvaluationConfig(timeout_seconds=120),
    )
    return evaluator, base, sealed


def test_correct_fix_passes(setup: tuple[Evaluator, Path, Path]) -> None:
    evaluator, base, _ = setup
    verdict = evaluator.evaluate(TASK, base, FIX_DIFF, RUN_ID)
    assert verdict.passed
    assert verdict.exit_code == 0
    assert verdict.tampered == ()


def test_no_change_fails(setup: tuple[Evaluator, Path, Path]) -> None:
    evaluator, base, _ = setup
    verdict = evaluator.evaluate(TASK, base, "", RUN_ID)
    assert not verdict.passed
    assert verdict.exit_code == 1


def test_hidden_check_output_stays_sealed(setup: tuple[Evaluator, Path, Path]) -> None:
    """Required test 13 (evaluator half): output goes to the sealed store, not the verdict."""
    evaluator, base, sealed = setup
    verdict = evaluator.evaluate(TASK, base, FIX_DIFF, RUN_ID)
    assert SENTINEL not in repr(verdict)
    recorded = (sealed / TASK / "check_output" / f"{RUN_ID}.txt").read_text(encoding="utf-8")
    assert SENTINEL in recorded
    assert "1 passed" in recorded


def test_editing_tests_is_tampering_and_never_runs(setup: tuple[Evaluator, Path, Path]) -> None:
    """Required test 3: a diff that edits tests or check paths yields tampered."""
    evaluator, base, sealed = setup
    sneaky = FIX_DIFF.replace("src/toykit/dates.py", "tests/test_smoke.py")
    verdict = evaluator.evaluate(TASK, base, sneaky, RUN_ID)
    assert not verdict.passed
    assert verdict.tampered == ("tests/test_smoke.py",)
    assert not (sealed / TASK / "check_output").exists()


def test_a_diff_that_does_not_apply_fails(setup: tuple[Evaluator, Path, Path]) -> None:
    evaluator, base, _ = setup
    verdict = evaluator.evaluate(TASK, base, FIX_DIFF.replace("year, day, month", "nope"), RUN_ID)
    assert not verdict.passed
    assert not verdict.diff_applied


def test_a_symlink_planted_by_the_diff_is_refused(
    setup: tuple[Evaluator, Path, Path], tmp_path: Path, docker_client, sandbox_image: str
) -> None:
    _, base, sealed = setup
    SealedWriter(sealed).write_task(
        "symlink-trap-0001",
        check_sh="#!/usr/bin/env bash\ntrue\n",
        hidden_tests={"checks/test_hidden.py": "def test_h():\n    pass\n"},
        target_commit="abc1234",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    # the diff creates `checks` as a symlink to a host directory; `checks` itself is not a
    # protected path, but it stands where a hidden test's directory goes, so the diff is
    # refused before any container starts (the copy step's symlink guard is the backstop)
    planted = "\n".join(
        [
            "diff --git a/checks b/checks",
            "new file mode 120000",
            "--- /dev/null",
            "+++ b/checks",
            "@@ -0,0 +1 @@",
            f"+{outside}",
            "\\ No newline at end of file",
            "",
        ]
    )
    evaluator = Evaluator(
        SealedStore(sealed),
        docker_client,
        sandbox_image,
        SandboxConfig(memory="512m"),
        EvaluationConfig(timeout_seconds=60),
    )
    verdict = evaluator.evaluate("symlink-trap-0001", base, planted, RUN_ID)
    assert not verdict.passed
    assert verdict.tampered == ("checks",)
    assert list(outside.iterdir()) == []


def test_environment_errors_are_reported(setup: tuple[Evaluator, Path, Path]) -> None:
    _, base, sealed = setup
    broken = Evaluator(
        SealedStore(sealed),
        setup[0]._client,
        "saddlefit-no-such-image:0",
        SandboxConfig(memory="512m"),
        EvaluationConfig(timeout_seconds=60),
    )
    verdict = broken.evaluate(TASK, base, FIX_DIFF, RUN_ID)
    assert not verdict.passed
    assert verdict.env_error
