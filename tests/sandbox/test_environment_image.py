"""Environment images against a real Docker daemon: built once, run as the sandbox user."""

from pathlib import Path

import pytest

from saddlefit.config import SandboxConfig
from saddlefit.sandbox.container import Sandbox
from saddlefit.sandbox.environment import ensure_environment

pytestmark = pytest.mark.docker


def test_an_environment_image_builds_and_is_reused(
    docker_client, sandbox_image: str, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    # nothing to install, so the build needs no network
    (repo / "requirements.txt").write_text("# no dependencies\n", encoding="utf-8")
    record = ensure_environment(docker_client, sandbox_image, repo, "envtest")
    assert record is not None
    try:
        built = docker_client.images.get(record.image)
        again = ensure_environment(docker_client, sandbox_image, repo, "envtest")
        assert again is not None
        assert docker_client.images.get(again.image).id == built.id  # reused, not rebuilt
        work = tmp_path / "work"
        work.mkdir()
        with Sandbox(
            docker_client,
            record.image,
            work,
            SandboxConfig(memory="256m"),
            run_timeout_s=60,
            labels={"saddlefit.test": "1"},
        ) as box:
            # the sandbox runs as the host's uid, never root; the build left no files behind
            result = box.exec('test "$(id -u)" != 0 && test ! -e /tmp/deps', 30)
        assert result.exit_code == 0, result.output
        assert built.attrs["Config"]["User"] == "student"
    finally:
        docker_client.images.remove(record.image, force=True)
