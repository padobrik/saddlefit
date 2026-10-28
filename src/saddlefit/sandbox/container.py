"""The student sandbox and the evaluation container (B11).

Each container runs with no network, as a non-root user, on a read-only root filesystem
with only ``/work`` (the run's workspace) and ``/tmp`` writable, under CPU, memory and pids
limits, with an allowlisted environment and wall-clock timeouts per command and per run.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from saddlefit.config import SandboxConfig
from saddlefit.errors import EnvironmentFailure

WORKDIR = "/work"
FALLBACK_USER = "1000:1000"
# Only these variables exist in the sandbox, with fixed values; nothing comes from the host.
SANDBOX_ENV: dict[str, str] = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/tmp",  # noqa: S108 - a path inside the container, on its private tmpfs
    "LANG": "C.UTF-8",
}
TMP_SIZE = "512m"
HARNESS_TOOLS = "/harness/tools"
KILL_GRACE_S = 5
HOST_MARGIN_S = 5
OUTPUT_LIMIT_BYTES = 1_000_000
MIN_NANO_CPUS = 10_000_000  # 0.01 CPU; Docker treats 0 as unlimited
MIN_MEMORY_BYTES = 6 * 1024 * 1024  # Docker's minimum; 0 would mean unlimited
_MEMORY = re.compile(r"^(?P<n>[0-9]+)(?P<unit>[bkmg]?)$")
_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


class SandboxError(EnvironmentFailure):
    """The sandbox could not be created or used (an environment error)."""


class _CappedOutput:
    """Keeps the head and tail of a byte stream, counting what was dropped in between."""

    def __init__(self, limit: int) -> None:
        self._half = limit // 2
        self._head = bytearray()
        self._tail = bytearray()
        self._dropped = 0
        self._carry = b""
        self.timeout_notice_seen = False
        self.error: Exception | None = None

    def add(self, chunk: bytes) -> None:
        # Look for the supervisor's notice before any truncation, across chunk boundaries.
        window = self._carry + chunk
        if not self.timeout_notice_seen and _TIMEOUT_NOTICE_BYTES.search(window):
            self.timeout_notice_seen = True
        self._carry = window[-_NOTICE_CARRY:]
        room = self._half - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        if not chunk:
            return
        self._tail += chunk
        overflow = len(self._tail) - self._half
        if overflow > 0:
            del self._tail[:overflow]
            self._dropped += overflow

    def text(self) -> str:
        head = self._head.decode("utf-8", errors="replace")
        tail = self._tail.decode("utf-8", errors="replace")
        if self._dropped:
            return f"{head}\n[... {self._dropped} bytes of output truncated ...]\n{tail}"
        return head + tail


def _close(stream: Any) -> None:
    """Close an exec stream and its HTTP response.

    ``CancellableStream.close`` is a no-op once the stream is exhausted, and only closing
    the response (the SDK keeps it on ``_response``) releases the upgraded socket.
    """
    for close in (
        getattr(stream, "close", None),
        getattr(getattr(stream, "_response", None), "close", None),
    ):
        if close is not None:
            with contextlib.suppress(Exception):  # best effort: it may already be gone
                close()


def _drain(stream: Any, buffer: _CappedOutput) -> None:
    try:
        for chunk in stream:
            buffer.add(chunk if isinstance(chunk, bytes) else str(chunk).encode())
    except Exception as exc:  # reported by the caller as a SandboxError
        buffer.error = exc
    finally:
        _close(stream)  # the SDK's stream holds an HTTP response open until closed


def memory_bytes(value: str) -> int:
    match = _MEMORY.match(value.lower())
    if match is None:
        raise SandboxError(f"invalid memory limit: {value!r}")
    return int(match["n"]) * _UNITS[match["unit"]]


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
    workspace: Path,
    limits: SandboxConfig,
    *,
    labels: dict[str, str] | None = None,
    harness_tools: Path | None = None,
) -> dict[str, Any]:
    """Keyword arguments for ``docker.containers.run`` that enforce every B11 limit."""
    if not workspace.is_absolute():
        raise SandboxError(f"workspace must be an absolute path: {workspace}")
    if not workspace.is_dir():
        raise SandboxError(f"workspace is not a directory: {workspace}")
    nano_cpus = int(limits.cpus * 1_000_000_000)
    if nano_cpus < MIN_NANO_CPUS:
        raise SandboxError(
            f"cpu limit too small, Docker would treat it as unlimited: {limits.cpus}"
        )
    if memory_bytes(limits.memory) < MIN_MEMORY_BYTES:
        raise SandboxError(f"memory limit below Docker's 6m minimum: {limits.memory!r}")
    if harness_tools is not None and not (harness_tools.is_absolute() and harness_tools.is_dir()):
        raise SandboxError(f"harness tools must be an absolute directory: {harness_tools}")
    volumes = {str(workspace): {"bind": WORKDIR, "mode": "rw"}}
    environment = dict(SANDBOX_ENV)
    if harness_tools is not None:
        # outside /work, so the scripts never show up in the student's diff
        volumes[str(harness_tools)] = {"bind": HARNESS_TOOLS, "mode": "ro"}
        environment["PATH"] = f"{HARNESS_TOOLS}:{environment['PATH']}"
    return {
        "command": ["sleep", "infinity"],
        "detach": True,
        "network_mode": "none",
        "user": sandbox_user(),
        "read_only": True,
        "tmpfs": {"/tmp": f"rw,nosuid,nodev,size={TMP_SIZE}"},  # noqa: S108 - container path
        "volumes": volumes,
        "working_dir": WORKDIR,
        "environment": environment,
        "nano_cpus": nano_cpus,
        "mem_limit": limits.memory,
        "memswap_limit": limits.memory,
        "pids_limit": limits.pids_limit,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "labels": {"saddlefit": "sandbox", **(labels or {})},
    }


# The notice may follow output that has no trailing newline, so it is not anchored.
_TIMEOUT_NOTICE_BYTES = re.compile(rb"timeout: sending signal \w+ to command ")
_NOTICE_CARRY = 64
_TIMEOUT_NOTICE = re.compile(r"timeout: sending signal \w+ to command [^\n]*(?:\n|$)")


def _strip_timeout_notice(output: str) -> tuple[str, bool]:
    """Remove ``timeout --verbose`` notices; report whether any was present."""
    stripped, count = _TIMEOUT_NOTICE.subn("", output)
    return stripped, count > 0


def _timed_command(command: str, timeout_s: float) -> list[str]:
    seconds = max(1, int(timeout_s))
    # --preserve-status: the command's own exit code survives, so "exit 124" is not a timeout
    return [
        "timeout",
        "--preserve-status",
        "--verbose",  # announces the signal it sends: the timeout event itself
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
        harness_tools: Path | None = None,
    ) -> None:
        self._client = client
        self._image = image
        self._options = container_options(
            workspace, limits, labels=labels, harness_tools=harness_tools
        )
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
    def running(self) -> bool:
        """False once the container is gone, for example after a runaway was killed."""
        return self._container is not None

    @property
    def remaining_s(self) -> float:
        return max(0.0, self._deadline - self._clock())

    def limit_to(self, deadline: float) -> None:
        """End the run by ``deadline`` (on this sandbox's clock) if that comes first."""
        self._deadline = min(self._deadline, deadline)

    def exec(self, command: str, timeout_s: float) -> ExecResult:
        """Run a shell command in ``/work``, bounded by its own and the run's time limit.

        The in-container ``timeout`` is backed by a host-side deadline: a process that
        escapes its group and holds the output pipe gets the whole container killed. Only
        the first and last ``OUTPUT_LIMIT_BYTES / 2`` bytes of output are kept.
        """
        if self._container is None:
            raise SandboxError("sandbox is not running")
        budget = min(timeout_s, self.remaining_s)
        if budget < 1:
            return ExecResult(
                exit_code=124, output="run time limit reached", timed_out=True, duration_s=0.0
            )
        api = self._client.api
        started = self._clock()
        try:
            exec_id = api.exec_create(
                self._container.id,
                _timed_command(command, budget),
                user=self._options["user"],
                workdir=WORKDIR,
                environment=dict(self._options["environment"]),
            )["Id"]
            stream = api.exec_start(exec_id, stream=True)
        except Exception as exc:
            raise SandboxError(f"exec failed: {shlex.quote(command)}: {exc}") from exc

        buffer = _CappedOutput(OUTPUT_LIMIT_BYTES)
        reader = threading.Thread(target=_drain, args=(stream, buffer), daemon=True)
        reader.start()
        reader.join(budget + KILL_GRACE_S + HOST_MARGIN_S)
        if reader.is_alive():
            _close(stream)
            self._kill_runaway()
            reader.join(HOST_MARGIN_S)
            return ExecResult(
                exit_code=137,
                output=buffer.text() + "\n[sandbox killed: command outlived its time limit]",
                timed_out=True,
                duration_s=self._clock() - started,
            )
        if buffer.error is not None:
            raise SandboxError(f"exec failed: {shlex.quote(command)}: {buffer.error}")
        try:
            code = int(api.exec_inspect(exec_id)["ExitCode"])
        except Exception as exc:
            raise SandboxError(f"exec failed: {shlex.quote(command)}: {exc}") from exc
        duration = self._clock() - started
        output, _ = _strip_timeout_notice(buffer.text())
        signalled = buffer.timeout_notice_seen
        # Only the supervisor knows a deadline fired: it announces the signal it sends. The
        # exit code is the command's own (--preserve-status), so a TERM handler exiting 0
        # still timed out and an early 124 or 137 did not; an early forged notice is ignored.
        timed_out = signalled and duration >= budget
        return ExecResult(exit_code=code, output=output, timed_out=timed_out, duration_s=duration)

    def _kill_runaway(self) -> None:
        container, self._container = self._container, None
        try:
            container.remove(force=True)
        except Exception as exc:
            raise SandboxError(f"cannot remove runaway sandbox container: {exc}") from exc

    def close(self) -> None:
        container, self._container = self._container, None
        if container is None:
            return
        try:
            container.remove(force=True)
        except Exception as exc:
            raise SandboxError(f"cannot remove sandbox container: {exc}") from exc
