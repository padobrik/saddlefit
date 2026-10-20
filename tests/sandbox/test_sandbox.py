"""Sandbox limits against a real Docker daemon (B11); includes required test 10."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from saddlefit.config import SandboxConfig
from saddlefit.sandbox.container import OUTPUT_LIMIT_BYTES, Sandbox

pytestmark = pytest.mark.docker

LIMITS = SandboxConfig(cpus=1, memory="256m", pids_limit=64)


@pytest.fixture
def sandbox(docker_client, sandbox_image: str, tmp_path: Path) -> Iterator[Sandbox]:
    (tmp_path / "hello.txt").write_text("hi\n", encoding="utf-8")
    with Sandbox(
        docker_client,
        sandbox_image,
        tmp_path,
        LIMITS,
        run_timeout_s=120,
        labels={"saddlefit.test": "1"},
    ) as box:
        yield box


def test_no_network(sandbox: Sandbox) -> None:
    """Required test 10: a connection attempt from the sandbox must fail."""
    probe = "python -c \"import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)\""
    result = sandbox.exec(probe, timeout_s=30)
    assert result.exit_code != 0
    assert "Network is unreachable" in result.output or "OSError" in result.output
    # No routes at all: only the header line of the IPv4 and IPv6 routing tables.
    routes = sandbox.exec("tail -n +2 /proc/net/route", timeout_s=10)
    assert routes.output.strip() == ""
    v6 = sandbox.exec("grep -v ' lo$' /proc/net/ipv6_route || true", timeout_s=10)
    assert v6.output.strip() == ""


def test_runs_as_non_root(sandbox: Sandbox) -> None:
    assert sandbox.exec("id -u", timeout_s=10).output.strip() != "0"


def test_only_work_and_tmp_are_writable(sandbox: Sandbox) -> None:
    assert sandbox.exec("cat hello.txt && echo ok > out.txt", timeout_s=10).exit_code == 0
    assert sandbox.exec("touch /tmp/x", timeout_s=10).exit_code == 0
    # /var/tmp is world-writable in the image, so only a read-only root stops this write.
    for path in ("/var/tmp/x", "/etc/x", "/usr/x", "/x"):  # noqa: S108 - container paths
        assert sandbox.exec(f"touch {path}", timeout_s=10).exit_code != 0, path
    root = sandbox.exec("awk '$2 == \"/\" {print $4}' /proc/mounts", timeout_s=10).output
    assert "ro" in root.strip().split(",")


def test_workspace_is_the_only_host_mount(sandbox: Sandbox, tmp_path: Path) -> None:
    sandbox.exec("echo from-sandbox > created.txt", timeout_s=10)
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "from-sandbox\n"
    mounts = sandbox.exec("cat /proc/self/mountinfo", timeout_s=10).output
    binds = [line.split()[4] for line in mounts.splitlines() if " - " in line]
    assert "/work" in binds
    assert not {"/var/run/docker.sock", "/root", "/Users", "/home/runner"} & set(binds)


def test_environment_is_allowlisted(
    docker_client, sandbox_image: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-host-secret-value")
    with Sandbox(docker_client, sandbox_image, tmp_path, LIMITS, run_timeout_s=60) as box:
        env = box.exec("env", timeout_s=10).output
    assert "sk-ant-host-secret-value" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "HOME=/tmp" in env


def test_resource_limits_apply(sandbox: Sandbox) -> None:
    assert sandbox.exec("cat /sys/fs/cgroup/pids.max", timeout_s=10).output.strip() == "64"
    memory = sandbox.exec("cat /sys/fs/cgroup/memory.max", timeout_s=10).output.strip()
    assert memory == str(256 * 1024 * 1024)
    cpus = sandbox.exec("cat /sys/fs/cgroup/cpu.max", timeout_s=10).output.split()
    assert cpus == ["100000", "100000"]


def test_command_timeout(sandbox: Sandbox) -> None:
    result = sandbox.exec("sleep 30", timeout_s=2)
    assert result.timed_out
    assert result.duration_s < 15


def test_a_command_exiting_124_is_not_a_timeout(sandbox: Sandbox) -> None:
    result = sandbox.exec("exit 124", timeout_s=30)
    assert result.exit_code == 124
    assert not result.timed_out


def test_capabilities_are_dropped(sandbox: Sandbox) -> None:
    # CapEff is empty for any non-root user; the bounding set is empty only with cap_drop ALL.
    status = sandbox.exec("grep CapBnd /proc/self/status", timeout_s=10).output
    assert status.split()[-1] == "0000000000000000"
    nnp = sandbox.exec("grep NoNewPrivs /proc/self/status", timeout_s=10).output
    assert nnp.split()[-1] == "1"


def test_huge_output_is_capped_on_the_host(sandbox: Sandbox) -> None:
    result = sandbox.exec("head -c 5000000 /dev/zero | tr '\\0' a; echo END", timeout_s=30)
    assert result.exit_code == 0
    assert len(result.output) < OUTPUT_LIMIT_BYTES + 200
    assert "bytes of output truncated" in result.output
    assert result.output.rstrip().endswith("END")


def test_process_escaping_its_group_does_not_block_the_host(sandbox: Sandbox) -> None:
    # setsid moves sleep out of timeout's process group; the call must still return on time
    # (the host-side deadline for a pipe that stays open is covered by a unit test)
    result = sandbox.exec("setsid sleep 120", timeout_s=2)
    assert result.timed_out
    assert result.duration_s < 2 + 5 + 5 + 5
