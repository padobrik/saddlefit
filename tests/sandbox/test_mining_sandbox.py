"""Mining runs candidate tests only inside the sandbox, never on the host (B3, B11)."""

from pathlib import Path

import pytest
from tests.fixtures.toyrepo.build import FIXES, build

from saddlefit.config import SandboxConfig
from saddlefit.tasks.mining import SandboxRunner, mine

pytestmark = pytest.mark.docker

LIMITS = SandboxConfig(cpus=1, memory="512m", pids_limit=128)


def test_toy_history_mined_in_the_sandbox(
    docker_client, sandbox_image: str, tmp_path: Path
) -> None:
    repo = build(tmp_path / "repo")
    report = mine(repo, SandboxRunner(docker_client, sandbox_image, LIMITS), limit=3)
    assert report.summary()["viable"] == 3
    assert {c.subject for c in report.viable} <= {fix[0] for fix in FIXES}


def test_candidate_tests_see_nothing_of_the_host(
    docker_client, sandbox_image: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "original-repo"
    repo.mkdir()
    host_tmp = tmp_path / "host-tmp"
    host_tmp.mkdir()
    monkeypatch.setenv("SADDLEFIT_TEST_SECRET", "x")
    monkeypatch.setenv("TMPDIR", str(host_tmp))
    snapshot = tmp_path / "snapshot"
    (snapshot / "tests").mkdir(parents=True)
    (snapshot / "tests" / "test_isolation.py").write_text(
        "import os, tempfile\n\n\ndef test_isolation():\n"
        f"    assert not os.path.exists({str(repo)!r})\n"
        "    assert 'SADDLEFIT_TEST_SECRET' not in os.environ\n"
        f"    assert not tempfile.gettempdir().startswith({str(host_tmp)!r})\n"
        "    assert os.getcwd() == '/work'\n",
        encoding="utf-8",
    )
    runner = SandboxRunner(docker_client, sandbox_image, LIMITS)
    assert runner.run_tests(snapshot, ["tests/test_isolation.py"]) == 0
