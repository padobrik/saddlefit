"""Runs candidate tests on the host: for Saddlefit's own tests on its toy repository only.

Saddlefit itself mines through ``SandboxRunner``; this helper keeps the git-level mining
tests fast and Docker-free.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HostRunner:
    python: str
    timeout_s: float = 300

    def run_tests(self, snapshot: Path, test_files: Sequence[str]) -> int:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(snapshot / "src"),
        }
        result = subprocess.run(
            [self.python, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", *test_files],
            cwd=snapshot,
            env=env,
            capture_output=True,
            timeout=self.timeout_s,
            check=False,
        )
        return result.returncode
