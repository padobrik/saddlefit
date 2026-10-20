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
    container_options,
    sandbox_user,
)

LIMITS = SandboxConfig(cpus=1.5, memory="512m", pids_limit=128)


class FakeContainer:
    def __init__(self, replies: list[Any]) -> None:
        self.replies = replies
        self.execs: list[dict[str, Any]] = []
        self.removed = False

    def exec_run(self, cmd: list[str], **kwargs: Any) -> Any:
        self.execs.append({"cmd": cmd, **kwargs})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def remove(self, force: bool) -> None:
        assert force
        self.removed = True


class FakeClient:
    def __init__(self, box: FakeContainer | Exception) -> None:
        self.box = box
        self.runs: list[tuple[str, dict[str, Any]]] = []
        self.containers = SimpleNamespace(run=self._run)

    def _run(self, image: str, **kwargs: Any) -> FakeContainer:
        self.runs.append((image, kwargs))
        if isinstance(self.box, Exception):
            raise self.box
        return self.box


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


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


def test_user_is_never_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.os, "getuid", lambda: 0, raising=False)
    monkeypatch.setattr(mod.os, "getgid", lambda: 0, raising=False)
    assert sandbox_user() == "1000:1000"
    monkeypatch.setattr(mod.os, "getuid", lambda: 1001, raising=False)
    monkeypatch.setattr(mod.os, "getgid", lambda: 121, raising=False)
    assert sandbox_user() == "1001:121"
    monkeypatch.delattr(mod.os, "getuid", raising=False)
    assert sandbox_user() == "1000:1000"


def test_exec_wraps_commands_in_a_timeout(tmp_path: Path) -> None:
    box = FakeContainer([(0, b"ok\n"), (124, b""), (137, b"Killed")])
    clock = Clock()
    with Sandbox(FakeClient(box), "img", tmp_path, LIMITS, run_timeout_s=100, clock=clock) as sb:
        result = sb.exec("pytest -q", timeout_s=30)
        assert (result.exit_code, result.output, result.timed_out) == (0, "ok\n", False)
        assert box.execs[0]["cmd"] == ["timeout", "--kill-after=5", "30", "bash", "-c", "pytest -q"]
        assert box.execs[0]["environment"] == SANDBOX_ENV
        assert box.execs[0]["workdir"] == "/work"
        assert sb.exec("sleep 99", timeout_s=5).timed_out
        # 137 without exhausting the budget is an OOM kill, not a timeout
        assert not sb.exec("big", timeout_s=5).timed_out
    assert box.removed


def test_run_deadline_caps_each_command(tmp_path: Path) -> None:
    box = FakeContainer([(0, b"")])
    clock = Clock()
    with Sandbox(FakeClient(box), "img", tmp_path, LIMITS, run_timeout_s=10, clock=clock) as sb:
        clock.now = 7.5
        sb.exec("x", timeout_s=60)
        assert box.execs[0]["cmd"][2] == "2"
        clock.now = 10
        late = sb.exec("y", timeout_s=60)
    assert late.timed_out
    assert late.exit_code == 124
    assert len(box.execs) == 1


def test_errors_become_sandbox_errors(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="cannot start"):
        Sandbox(
            FakeClient(RuntimeError("no image")), "img", tmp_path, LIMITS, run_timeout_s=5
        ).start()
    box = FakeContainer([RuntimeError("gone")])
    sb = Sandbox(FakeClient(box), "img", tmp_path, LIMITS, run_timeout_s=5)
    with pytest.raises(SandboxError, match="not running"):
        sb.exec("x", timeout_s=1)
    sb.start()
    with pytest.raises(SandboxError, match="exec failed"):
        sb.exec("x", timeout_s=5)
    sb.close()
    sb.close()
