"""Lint proposed tool scripts inside the sandbox: ruff for Python, shellcheck for shell (B8).

The scripts are copied into a fresh workspace and checked in a container with no network,
so nothing a proposal writes runs on the host, and the linters see no project config.
"""

from __future__ import annotations

import shlex
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Final

from saddlefit.config import SandboxConfig
from saddlefit.sandbox.container import Sandbox

LINT_TIMEOUT_S: Final = 120
# --isolated: no config file in the workspace can switch rules off
_COMMANDS: Final = {
    ".py": "ruff check --isolated --no-cache --output-format concise --",
    ".sh": "shellcheck --",
}


class SandboxLinter:
    """A ``ScriptLinter`` that runs the linters in the sandbox image."""

    def __init__(self, client: Any, image: str, limits: SandboxConfig) -> None:
        self._client = client
        self._image = image
        self._limits = limits

    def lint(self, scripts: Mapping[str, bytes]) -> list[str]:
        by_suffix: dict[str, list[str]] = {}
        for path in sorted(scripts):
            by_suffix.setdefault(PurePosixPath(path).suffix, []).append(path)
        unknown = sorted(set(by_suffix) - set(_COMMANDS))
        if unknown:
            raise ValueError(f"no linter for {unknown}")
        problems: list[str] = []
        with tempfile.TemporaryDirectory(prefix="saddlefit-lint-") as tmp:
            workspace = Path(tmp)
            for path, data in scripts.items():
                target = workspace / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            with Sandbox(
                self._client, self._image, workspace, self._limits, run_timeout_s=LINT_TIMEOUT_S
            ) as sandbox:
                for suffix, paths in sorted(by_suffix.items()):
                    command = f"{_COMMANDS[suffix]} {shlex.join(paths)}"
                    result = sandbox.exec(command, timeout_s=sandbox.remaining_s)
                    if result.timed_out:
                        problems.append(f"{_COMMANDS[suffix].split()[0]} timed out")
                    elif result.exit_code != 0:
                        problems.append(result.output.strip())
        return problems
