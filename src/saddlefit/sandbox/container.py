"""The student sandbox and the evaluation container (B11).

Each container runs with no network, as a non-root user, on a read-only root filesystem
with only ``/work`` (the run's workspace) and ``/tmp`` writable, under CPU, memory and pids
limits, with an allowlisted environment and wall-clock timeouts per command and per run.
"""

from __future__ import annotations

import os
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from saddlefit.config import SandboxConfig

WORKDIR = "/work"
FALLBACK_USER = "1000:1000"
# Only these variables exist in the sandbox, with fixed values; nothing comes from the host.
SANDBOX_ENV: dict[str, str] = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/tmp",  # noqa: S108 - a path inside the container, on its private tmpfs
    "LANG": "C.UTF-8",
}
TMP_SIZE = "512m"
KILL_GRACE_S = 5


class SandboxError(Exception):
    """The sandbox could not be created or used (an environment error)."""


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    output: str
    timed_out: bool
    duration_s: float


def sandbox_user() -> str:
    """The host's uid:gid, so the bind-mounted workspace stays writable; never root."""
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is None or getgid is None or getuid() == 0:
        return FALLBACK_USER
    return f"{getuid()}:{getgid()}"


def container_options(
    workspace: Path, limits: SandboxConfig, *, labels: dict[str, str] | None = None
) -> dict[str, Any]:
    """Keyword arguments for ``docker.containers.run`` that enforce every B11 limit."""
    if not workspace.is_absolute():
        raise SandboxError(f"workspace must be an absolute path: {workspace}")
    if not workspace.is_dir():
        raise SandboxError(f"workspace is not a directory: {workspace}")
    return {
        "command": ["sleep", "infinity"],
        "detach": True,
        "network_mode": "none",
        "user": sandbox_user(),
        "read_only": True,
        "tmpfs": {"/tmp": f"rw,nosuid,nodev,size={TMP_SIZE}"},  # noqa: S108 - container path
        "volumes": {str(workspace): {"bind": WORKDIR, "mode": "rw"}},
        "working_dir": WORKDIR,
        "environment": dict(SANDBOX_ENV),
        "nano_cpus": int(limits.cpus * 1_000_000_000),
        "mem_limit": limits.memory,
        "memswap_limit": limits.memory,
        "pids_limit": limits.pids_limit,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "labels": {"saddlefit": "sandbox", **(labels or {})},
    }


def _timed_command(command: str, timeout_s: float) -> list[str]:
    seconds = max(1, int(timeout_s))
    return [
        "timeout",
        f"--kill-after={KILL_GRACE_S}",
        str(seconds),
        "bash",
        "-c",
        command,
    ]


class Sandbox:
    """One running container bound to one workspace; use as a context manager."""

    def __init__(
        self,
        client: Any,
        image: str,
        workspace: Path,
        limits: SandboxConfig,
        *,
        run_timeout_s: float,
        labels: dict[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._image = image
        self._options = container_options(workspace, limits, labels=labels)
        self._run_timeout_s = run_timeout_s
        self._clock = clock
        self._container: Any = None
        self._deadline = 0.0

    def __enter__(self) -> Sandbox:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def start(self) -> None:
        try:
            self._container = self._client.containers.run(self._image, **self._options)
        except Exception as exc:
            raise SandboxError(f"cannot start sandbox from {self._image}: {exc}") from exc
        self._deadline = self._clock() + self._run_timeout_s

    @property
    def remaining_s(self) -> float:
        return max(0.0, self._deadline - self._clock())

    def exec(self, command: str, timeout_s: float) -> ExecResult:
        """Run a shell command in ``/work``, bounded by its own and the run's time limit."""
        if self._container is None:
            raise SandboxError("sandbox is not running")
        budget = min(timeout_s, self.remaining_s)
        if budget < 1:
            return ExecResult(
                exit_code=124, output="run time limit reached", timed_out=True, duration_s=0.0
            )
        started = self._clock()
        try:
            code, raw = self._container.exec_run(
                _timed_command(command, budget),
                user=self._options["user"],
                workdir=WORKDIR,
                environment=dict(SANDBOX_ENV),
            )
        except Exception as exc:
            raise SandboxError(f"exec failed: {shlex.quote(command)}: {exc}") from exc
        output = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        duration = self._clock() - started
        # coreutils ``timeout`` exits 124, or 137 when it had to kill; 137 alone may be OOM.
        timed_out = code == 124 or (code == 137 and duration >= budget)
        return ExecResult(
            exit_code=int(code), output=output, timed_out=timed_out, duration_s=duration
        )

    def close(self) -> None:
        container, self._container = self._container, None
        if container is None:
            return
        try:
            container.remove(force=True)
        except Exception as exc:
            raise SandboxError(f"cannot remove sandbox container: {exc}") from exc
