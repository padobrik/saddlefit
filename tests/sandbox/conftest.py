import os
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

docker = pytest.importorskip("docker")


def _unix_socket_path() -> str | None:
    host = os.environ.get("DOCKER_HOST", "")
    if host.startswith("unix://"):
        return host.removeprefix("unix://")
    if host:
        return None
    for candidate in (Path.home() / ".docker/run/docker.sock", Path("/var/run/docker.sock")):
        if candidate.exists():
            return str(candidate)
    return "/var/run/docker.sock"


def _daemon_unreachable_reason() -> str | None:
    # Probe the socket ourselves: the SDK leaks the socket when a connect fails.
    path = _unix_socket_path()
    if path is None or os.name == "nt":
        return None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        try:
            sock.connect(path)
        except OSError as exc:
            return f"cannot connect to {path}: {exc}"
    return None


@pytest.fixture(scope="session")
def docker_client() -> Iterator["docker.DockerClient"]:
    reason = _daemon_unreachable_reason()
    if reason is not None:
        # CI sets SADDLEFIT_REQUIRE_DOCKER so a missing daemon fails the sandbox job.
        if os.environ.get("SADDLEFIT_REQUIRE_DOCKER") == "1":
            pytest.fail(f"Docker is required but unavailable: {reason}")
        pytest.skip(f"Docker is not available: {reason}")
    client = docker.from_env()
    yield client
    client.close()


@pytest.fixture(scope="session")
def sandbox_image(docker_client: "docker.DockerClient") -> str:
    """The sandbox image under test. Building it needs the network, so tests never do."""
    image = os.environ.get("SADDLEFIT_SANDBOX_IMAGE", "saddlefit-sandbox:test")
    try:
        docker_client.images.get(image)
    except docker.errors.ImageNotFound:
        hint = f"sandbox image {image!r} not found; build it with: docker build -t {image} sandbox/"
        if os.environ.get("SADDLEFIT_REQUIRE_DOCKER") == "1":
            pytest.fail(hint)
        pytest.skip(hint)
    return image
