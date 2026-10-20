from pathlib import Path

import pytest

from saddlefit.config import EvaluationConfig, SandboxConfig
from saddlefit.eval.evaluator import Evaluator, PlantedSymlinkError, _inside
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
