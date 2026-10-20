import pytest

pytestmark = pytest.mark.docker

PROBE = (
    "import socket,sys\n"
    "try:\n"
    "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
    "except OSError:\n"
    "    sys.exit(0)\n"
    "sys.exit(1)\n"
)


def test_container_without_network_cannot_connect(docker_client) -> None:
    out = docker_client.containers.run(
        "python:3.12-slim",
        ["python", "-c", PROBE],
        network_mode="none",
        remove=True,
        stdout=True,
        stderr=True,
    )
    assert out == b""
