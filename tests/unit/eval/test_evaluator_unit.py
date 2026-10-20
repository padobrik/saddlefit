import subprocess
from pathlib import Path

import pytest

from saddlefit.config import EvaluationConfig, SandboxConfig
from saddlefit.eval import evaluator as evaluator_module
from saddlefit.eval.evaluator import MAX_DIFF_BYTES, Evaluator, PlantedSymlinkError, _inside
from saddlefit.eval.sealed import SealedStore
from saddlefit.tasks.sealing import SealedWriter

DIFF = (
    "diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
)


def test_tampering_is_decided_before_any_container(tmp_path: Path) -> None:
    SealedWriter(tmp_path).write_task(
        "t-0001", check_sh="true\n", hidden_tests={"tests/test_h.py": "x"}, target_commit="abc1234"
    )
    # no docker client at all: a tampered diff must never need one
    evaluator = Evaluator(SealedStore(tmp_path), None, "img", SandboxConfig(), EvaluationConfig())
    verdict = evaluator.evaluate("t-0001", tmp_path, DIFF, "01JB2Y8Q4V9N1K3D7M5R6T0W2X")
    assert (verdict.passed, verdict.tampered, verdict.exit_code) == (
        False,
        ("tests/test_a.py",),
        None,
    )


def test_inside_guards_symlinks(tmp_path: Path) -> None:
    work = tmp_path / "work"
    (work / "real").mkdir(parents=True)
    assert _inside(work, "real/x.py") == work / "real" / "x.py"
    (work / "link").symlink_to(tmp_path)
    with pytest.raises(PlantedSymlinkError, match="link is a symlink"):
        _inside(work, "link/x.py")
    (work / "file.py").symlink_to(tmp_path / "elsewhere.py")
    assert _inside(work, "file.py") == work / "file.py"
    assert not (work / "file.py").exists()  # the planted link itself was removed


@pytest.mark.parametrize("path", [".saddlefit-check.sh", ".saddlefit-student.diff"])
def test_a_diff_touching_the_evaluator_files_is_tampering(tmp_path: Path, path: str) -> None:
    SealedWriter(tmp_path).write_task(
        "t-0001", check_sh="true\n", hidden_tests={"tests/test_h.py": "x"}, target_commit="abc1234"
    )
    diff = f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
    evaluator = Evaluator(SealedStore(tmp_path), None, "img", SandboxConfig(), EvaluationConfig())
    verdict = evaluator.evaluate("t-0001", tmp_path, diff, "01JB2Y8Q4V9N1K3D7M5R6T0W2X")
    assert verdict.tampered == (path,)


@pytest.mark.parametrize("path", ["tests/test_h.py/inner.py", "tests"])
def test_a_diff_in_the_way_of_a_hidden_test_is_tampering(tmp_path: Path, path: str) -> None:
    SealedWriter(tmp_path).write_task(
        "t-0001", check_sh="true\n", hidden_tests={"tests/test_h.py": "x"}, target_commit="abc1234"
    )
    diff = f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
    evaluator = Evaluator(SealedStore(tmp_path), None, "img", SandboxConfig(), EvaluationConfig())
    verdict = evaluator.evaluate("t-0001", tmp_path, diff, "01JB2Y8Q4V9N1K3D7M5R6T0W2X")
    assert verdict.tampered == (path,)


def _store(tmp_path: Path) -> SealedStore:
    SealedWriter(tmp_path / "sealed").write_task(
        "t-0001", check_sh="true\n", hidden_tests={"tests/test_h.py": "x"}, target_commit="abc1234"
    )
    return SealedStore(tmp_path / "sealed")


def test_a_headerless_diff_cannot_slip_past_the_tamper_check(tmp_path: Path) -> None:
    headerless = "--- a/tests/test_a.py\n+++ b/tests/test_a.py\n@@ -1 +1 @@\n-x\n+y\n"
    evaluator = Evaluator(_store(tmp_path), None, "img", SandboxConfig(), EvaluationConfig())
    verdict = evaluator.evaluate("t-0001", tmp_path, headerless, "01JB2Y8Q4V9N1K3D7M5R6T0W2X")
    assert verdict.tampered == ("tests/test_a.py",)


def test_a_diff_git_cannot_parse_fails_before_any_container(tmp_path: Path) -> None:
    evaluator = Evaluator(_store(tmp_path), None, "img", SandboxConfig(), EvaluationConfig())
    verdict = evaluator.evaluate("t-0001", tmp_path, "not a patch\n", "01JB2Y8Q4V9N1K3D7M5R6T0W2X")
    assert (verdict.passed, verdict.diff_applied, verdict.exit_code) == (False, False, None)


class _NoContainers:
    class containers:
        @staticmethod
        def run(*args: object, **kwargs: object) -> object:
            raise RuntimeError("daemon unavailable")


def test_environment_errors_are_reported(tmp_path: Path) -> None:
    base = tmp_path / "base"
    (base / "src").mkdir(parents=True)
    (base / "src" / "a.py").write_text("x\n", encoding="utf-8")
    diff = "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    evaluator = Evaluator(
        _store(tmp_path), _NoContainers(), "img", SandboxConfig(), EvaluationConfig()
    )
    verdict = evaluator.evaluate("t-0001", base, diff, "01JB2Y8Q4V9N1K3D7M5R6T0W2X")
    assert not verdict.passed
    assert verdict.env_error is not None
    assert "daemon unavailable" in verdict.env_error


def test_an_oversized_diff_fails_before_any_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_git(*args: object, **kwargs: object) -> object:
        raise AssertionError("git must not run on an oversized diff")

    monkeypatch.setattr(evaluator_module.subprocess, "run", no_git)
    diff = "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-x\n+" + "y" * MAX_DIFF_BYTES + "\n"
    evaluator = Evaluator(_store(tmp_path), None, "img", SandboxConfig(), EvaluationConfig())
    verdict = evaluator.evaluate("t-0001", tmp_path, diff, "01JB2Y8Q4V9N1K3D7M5R6T0W2X")
    assert (verdict.passed, verdict.diff_applied) == (False, False)


def test_a_slow_diff_parse_counts_as_a_diff_that_does_not_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def too_slow(*args: object, **kwargs: object) -> object:
        assert kwargs.get("timeout"), "the host-side parse needs a timeout"
        raise subprocess.TimeoutExpired(cmd="git", timeout=float(kwargs["timeout"]))  # type: ignore[arg-type]

    monkeypatch.setattr(evaluator_module.subprocess, "run", too_slow)
    diff = "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    evaluator = Evaluator(_store(tmp_path), None, "img", SandboxConfig(), EvaluationConfig())
    verdict = evaluator.evaluate("t-0001", tmp_path, diff, "01JB2Y8Q4V9N1K3D7M5R6T0W2X")
    assert (verdict.passed, verdict.diff_applied) == (False, False)


@pytest.mark.parametrize("bad", [chr(0xD800), chr(0xDC80)], ids=["high", "low-escape"])
def test_a_diff_that_is_not_utf8_fails_without_a_crash(tmp_path: Path, bad: str) -> None:
    diff = f"--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-x\n+{bad}\n"
    evaluator = Evaluator(_store(tmp_path), None, "img", SandboxConfig(), EvaluationConfig())
    verdict = evaluator.evaluate("t-0001", tmp_path, diff, "01JB2Y8Q4V9N1K3D7M5R6T0W2X")
    assert (verdict.passed, verdict.diff_applied) == (False, False)


def test_the_size_check_comes_before_the_python_diff_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_parsing(diff: str) -> set[str]:
        raise AssertionError("an oversized diff must not be parsed")

    monkeypatch.setattr(evaluator_module, "diff_paths", no_parsing)
    diff = "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-x\n+" + "y" * MAX_DIFF_BYTES + "\n"
    evaluator = Evaluator(_store(tmp_path), None, "img", SandboxConfig(), EvaluationConfig())
    verdict = evaluator.evaluate("t-0001", tmp_path, diff, "01JB2Y8Q4V9N1K3D7M5R6T0W2X")
    assert verdict.diff_applied is False
