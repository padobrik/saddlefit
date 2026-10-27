import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from saddlefit.config import SandboxConfig
from saddlefit.sandbox import container as mod
from saddlefit.sandbox.container import (
    SANDBOX_ENV,
    Sandbox,
    SandboxError,
    _CappedOutput,
    container_options,
    memory_bytes,
    sandbox_user,
)

LIMITS = SandboxConfig(cpus=1.5, memory="512m", pids_limit=128)


class FakeAPI:
    """Scripted ``client.api``: each exec yields chunks and then reports an exit code."""

    def __init__(self, scripts: list[Any]) -> None:
        self.scripts = scripts
        self.created: list[dict[str, Any]] = []
        self.current: Any = None
        self.clock: Any = None  # advanced by each script's "elapsed" seconds

    def exec_create(self, container_id: str, cmd: list[str], **kwargs: Any) -> dict[str, str]:
        self.created.append({"container": container_id, "cmd": cmd, **kwargs})
        self.current = self.scripts.pop(0)
        if isinstance(self.current, Exception):
            raise self.current
        return {"Id": f"exec{len(self.created)}"}

    def exec_start(self, exec_id: str, stream: bool) -> Any:
        assert stream
        if self.clock is not None:
            self.clock.now += self.current.get("elapsed", 0.0)
        chunks = self.current["chunks"]
        if isinstance(chunks, Exception):
            raise chunks
        return chunks

    def exec_inspect(self, exec_id: str) -> dict[str, int]:
        return {"ExitCode": self.current["code"]}


class FakeContainer:
    id = "c0ffee"

    def __init__(self) -> None:
        self.removed = False

    def remove(self, force: bool) -> None:
        assert force
        self.removed = True


class FakeClient:
    def __init__(self, scripts: list[Any], fail_start: Exception | None = None) -> None:
        self.box = FakeContainer()
        self.api = FakeAPI(scripts)
        self.fail_start = fail_start
        self.containers = SimpleNamespace(run=self._run)

    def _run(self, image: str, **kwargs: Any) -> FakeContainer:
        if self.fail_start:
            raise self.fail_start
        return self.box


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def done(code: int, *chunks: bytes) -> dict[str, Any]:
    return {"chunks": list(chunks), "code": code}


def test_options_enforce_every_limit(tmp_path: Path) -> None:
    opts = container_options(tmp_path, LIMITS, labels={"run": "r1"})
    assert opts["network_mode"] == "none"
    assert opts["read_only"] is True
    assert set(opts["tmpfs"]) == {"/tmp"}  # noqa: S108 - container path
    assert opts["volumes"] == {str(tmp_path): {"bind": "/work", "mode": "rw"}}
    assert opts["environment"] == SANDBOX_ENV
    assert set(SANDBOX_ENV) == {"PATH", "HOME", "LANG"}
    assert opts["nano_cpus"] == 1_500_000_000
    assert opts["mem_limit"] == opts["memswap_limit"] == "512m"
    assert opts["pids_limit"] == 128
    assert opts["cap_drop"] == ["ALL"]
    assert opts["security_opt"] == ["no-new-privileges"]
    assert opts["labels"] == {"saddlefit": "sandbox", "run": "r1"}
    assert not opts["user"].startswith("0:")


@pytest.mark.parametrize("bad", [Path("relative"), Path("/definitely/not/here")])
def test_workspace_must_be_an_existing_absolute_dir(bad: Path) -> None:
    with pytest.raises(SandboxError):
        container_options(bad, LIMITS)


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        (SandboxConfig(cpus=0.000000001), "cpu limit too small"),
        (SandboxConfig(memory="0"), "6m minimum"),
        (SandboxConfig(memory="0g"), "6m minimum"),
        (SandboxConfig(memory="5m"), "6m minimum"),
    ],
)
def test_limits_docker_would_treat_as_unlimited_are_rejected(
    tmp_path: Path, limits: SandboxConfig, message: str
) -> None:
    with pytest.raises(SandboxError, match=message):
        container_options(tmp_path, limits)


def test_memory_parsing() -> None:
    assert memory_bytes("4g") == 4 * 1024**3
    assert memory_bytes("512M") == 512 * 1024**2
    assert memory_bytes("1024") == 1024
    with pytest.raises(SandboxError, match="invalid memory"):
        memory_bytes("lots")


def test_user_is_never_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.os, "getuid", lambda: 0, raising=False)
    monkeypatch.setattr(mod.os, "getgid", lambda: 0, raising=False)
    assert sandbox_user() == "1000:1000"
    monkeypatch.setattr(mod.os, "getuid", lambda: 1001, raising=False)
    monkeypatch.setattr(mod.os, "getgid", lambda: 121, raising=False)
    assert sandbox_user() == "1001:121"
    monkeypatch.delattr(mod.os, "getuid", raising=False)
    assert sandbox_user() == "1000:1000"


MARKER = b"timeout: sending signal TERM to command \xe2\x80\x98bash\xe2\x80\x99\n"


def test_exec_streams_output_and_reads_the_exit_code(tmp_path: Path) -> None:
    client = FakeClient(
        [
            done(0, b"o", b"k\n"),
            {"chunks": [b"partial\n", MARKER], "code": 143, "elapsed": 5.2},  # killed by TERM
            {"chunks": [MARKER], "code": 0, "elapsed": 5.1},  # TERM handler exited 0
            {"chunks": [], "code": 124, "elapsed": 0.1},  # the command itself exited 124
            {"chunks": [b"Killed"], "code": 137, "elapsed": 0.1},  # OOM before the deadline
            {"chunks": [MARKER], "code": 1, "elapsed": 0.1},  # a forged marker, early
        ]
    )
    clock = Clock()
    client.api.clock = clock
    with Sandbox(client, "img", tmp_path, LIMITS, run_timeout_s=100, clock=clock) as sb:
        result = sb.exec("pytest -q", timeout_s=30)
        assert (result.exit_code, result.output, result.timed_out) == (0, "ok\n", False)
        call = client.api.created[0]
        assert call["container"] == "c0ffee"
        assert call["cmd"] == [
            "timeout",
            "--preserve-status",
            "--verbose",
            "--kill-after=5",
            "30",
            "bash",
            "-c",
            "pytest -q",
        ]
        assert call["environment"] == SANDBOX_ENV
        assert call["workdir"] == "/work"
        killed = sb.exec("sleep 99", timeout_s=5)
        assert (killed.timed_out, killed.output) == (True, "partial\n")
        handled = sb.exec("trap 'exit 0' TERM; sleep 99", timeout_s=5)
        assert (handled.exit_code, handled.timed_out) == (0, True)
        early = sb.exec("exit 124", timeout_s=5)
        assert (early.exit_code, early.timed_out) == (124, False)
        assert not sb.exec("big", timeout_s=5).timed_out
        assert not sb.exec("echo forged", timeout_s=5).timed_out


def test_run_deadline_caps_each_command(tmp_path: Path) -> None:
    client = FakeClient([done(0)])
    clock = Clock()
    with Sandbox(client, "img", tmp_path, LIMITS, run_timeout_s=10, clock=clock) as sb:
        clock.now = 7.5
        sb.exec("x", timeout_s=60)
        assert client.api.created[0]["cmd"][4] == "2"
        clock.now = 10
        late = sb.exec("y", timeout_s=60)
    assert late.timed_out
    assert late.exit_code == 124
    assert len(client.api.created) == 1


def test_host_deadline_kills_a_stream_that_never_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "KILL_GRACE_S", 0)
    monkeypatch.setattr(mod, "HOST_MARGIN_S", 0.05)
    release = threading.Event()

    def endless() -> Any:
        yield b"partial"
        release.wait(5)

    client = FakeClient([{"chunks": endless(), "code": 0}])
    sb = Sandbox(client, "img", tmp_path, LIMITS, run_timeout_s=100)
    sb.start()
    result = sb.exec("setsid sleep 999", timeout_s=1)
    release.set()
    assert result.timed_out
    assert result.exit_code == 137
    assert result.output.startswith("partial")
    assert "sandbox killed" in result.output
    assert client.box.removed
    with pytest.raises(SandboxError, match="not running"):
        sb.exec("true", timeout_s=1)


def test_output_keeps_head_and_tail() -> None:
    buf = _CappedOutput(10)
    for chunk in (b"abc", b"defgh", b"ijklmnop", b"qrst"):
        buf.add(chunk)
    assert buf.text() == "abcde\n[... 10 bytes of output truncated ...]\npqrst"
    small = _CappedOutput(10)
    small.add(b"abcdefg")
    assert small.text() == "abcdefg"


def test_errors_become_sandbox_errors(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="cannot start"):
        Sandbox(
            FakeClient([], fail_start=RuntimeError("no image")),
            "img",
            tmp_path,
            LIMITS,
            run_timeout_s=5,
        ).start()
    client = FakeClient(
        [
            RuntimeError("gone"),
            {"chunks": RuntimeError("reset"), "code": 0},
            {"chunks": iter_raising(), "code": 0},
        ]
    )
    sb = Sandbox(client, "img", tmp_path, LIMITS, run_timeout_s=50)
    with pytest.raises(SandboxError, match="not running"):
        sb.exec("x", timeout_s=1)
    sb.start()
    for _ in range(3):
        with pytest.raises(SandboxError, match="exec failed"):
            sb.exec("x", timeout_s=5)
    sb.close()
    sb.close()


def iter_raising() -> Any:
    yield b"x"
    raise ConnectionResetError("stream broke")


class Closable:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class ClosingStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False
        self._response = Closable()

    def __iter__(self) -> Any:
        return iter(self.chunks)

    def close(self) -> None:
        self.closed = True


def test_exec_stream_is_closed_after_reading(tmp_path: Path) -> None:
    stream = ClosingStream([b"ok"])
    client = FakeClient([{"chunks": stream, "code": 0}])
    with Sandbox(client, "img", tmp_path, LIMITS, run_timeout_s=10) as sb:
        assert sb.exec("x", timeout_s=5).output == "ok"
    assert stream.closed
    assert stream._response.closed


def test_timeout_notice_is_seen_even_when_truncated_away() -> None:
    buf = _CappedOutput(20)
    buf.add(b"head" + MARKER[:10])
    buf.add(MARKER[10:])  # split across chunks
    buf.add(b"x" * 500)  # pushes the notice out of the kept head and tail
    assert buf.timeout_notice_seen
    assert "sending signal" not in buf.text()


def test_no_notice_no_flag() -> None:
    buf = _CappedOutput(20)
    buf.add(b"timeout: sending nothing\n")
    assert not buf.timeout_notice_seen


def test_harness_tools_are_mounted_read_only_on_path(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    opts = container_options(work, LIMITS, harness_tools=tools)
    assert opts["volumes"][str(tools)] == {"bind": "/harness/tools", "mode": "ro"}
    assert opts["environment"]["PATH"].startswith("/harness/tools:")
    assert "/harness" not in container_options(work, LIMITS)["environment"]["PATH"]
    with pytest.raises(SandboxError, match="absolute directory"):
        container_options(work, LIMITS, harness_tools=tmp_path / "missing")
