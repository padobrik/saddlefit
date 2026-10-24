"""Generated tasks are kept only if they fail without the reference and pass with it (B13)."""

import shutil
from pathlib import Path

import pytest
from tests.fixtures.toyrepo.build import build

from saddlefit.config import EvaluationConfig, SandboxConfig
from saddlefit.eval.evaluator import Evaluator
from saddlefit.eval.sealed import SealedStore
from saddlefit.tasks.acceptance import accept, check_script, seal_generated
from saddlefit.tasks.generator import GeneratedTask

pytestmark = pytest.mark.docker

TASK = "generated-shout-0001"
SOURCE = "src/toykit/shout.py"
HIDDEN = {
    "tests/test_shout_generated.py": (
        "from toykit.shout import shout\n\n\n"
        "def test_shout_adds_emphasis():\n"
        "    assert shout('hi there') == 'HI THERE!'\n"
    )
}
PATCH = "\n".join(
    [
        f"diff --git a/{SOURCE} b/{SOURCE}",
        "new file mode 100644",
        "--- /dev/null",
        f"+++ b/{SOURCE}",
        "@@ -0,0 +1,2 @@",
        "+def shout(text: str) -> str:",
        "+    return text.upper() + '!'",
        "",
    ]
)


def task(**changes: object) -> GeneratedTask:
    fields: dict[str, object] = {
        "task_md": "# Add shout\n",
        "hidden_tests": HIDDEN,
        "reference_patch": PATCH,
        "visible_check": "true",
    }
    return GeneratedTask(**{**fields, **changes})  # type: ignore[arg-type]


@pytest.fixture
def setup(tmp_path: Path, docker_client, sandbox_image: str) -> tuple[Evaluator, Path, Path]:
    repo = build(tmp_path / "repo")
    base = tmp_path / "base"
    shutil.copytree(repo, base, ignore=shutil.ignore_patterns(".git"))
    sealed = tmp_path / "sealed"
    evaluator = Evaluator(
        SealedStore(sealed),
        docker_client,
        sandbox_image,
        SandboxConfig(memory="512m"),
        EvaluationConfig(timeout_seconds=120),
    )
    return evaluator, base, sealed


def judge(setup: tuple[Evaluator, Path, Path], generated: GeneratedTask) -> tuple[bool, str]:
    evaluator, base, sealed = setup
    seal_generated(sealed, TASK, generated)
    result = accept(evaluator, TASK, base, generated)
    return result.accepted, result.reason


def test_a_task_that_fails_without_and_passes_with_is_accepted(
    setup: tuple[Evaluator, Path, Path],
) -> None:
    accepted, reason = judge(setup, task())
    assert accepted, reason
    assert (setup[2] / TASK / "reference.patch").read_text(encoding="utf-8") == PATCH


def test_tests_that_already_pass_are_refused(setup: tuple[Evaluator, Path, Path]) -> None:
    trivial = {"tests/test_shout_generated.py": "def test_nothing():\n    pass\n"}
    assert judge(setup, task(hidden_tests=trivial)) == (
        False,
        "the hidden tests pass without the reference patch",
    )


def test_a_reference_that_does_not_fix_it_is_refused(setup: tuple[Evaluator, Path, Path]) -> None:
    wrong = PATCH.replace("+ '!'", "+ '?'")
    assert judge(setup, task(reference_patch=wrong)) == (
        False,
        "the hidden tests fail with the reference patch",
    )


def test_a_reference_that_does_not_apply_is_refused(setup: tuple[Evaluator, Path, Path]) -> None:
    broken = PATCH.replace("new file mode 100644\n--- /dev/null", "--- a/" + SOURCE)
    accepted, reason = judge(setup, task(reference_patch=broken))
    assert not accepted
    assert reason == "the reference patch does not apply to the base snapshot"


def test_the_check_script_runs_the_hidden_tests_sorted() -> None:
    script = check_script(["tests/test_b.py", "tests/test_a.py"])
    assert script.endswith("tests/test_a.py tests/test_b.py\n")
