"""Read access to the sealed store: evaluator only (B3, B10).

Nothing outside ``saddlefit.eval`` may import this module; a test enforces it. Hidden
tests, ``check.sh``, reference solutions, canaries and hidden-check output are read here
and nowhere else.
"""

from __future__ import annotations

import re
from pathlib import Path

from saddlefit.tasks.sealing import CANARY_PREFIX

_RUN_ID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")  # a ULID


class SealedStoreError(Exception):
    """A sealed task is missing or malformed."""


class SealedStore:
    """The evaluator's view of ``tasks/sealed/``."""

    def __init__(self, sealed_root: Path) -> None:
        self._root = sealed_root

    def _task_dir(self, task_id: str) -> Path:
        path = self._root / task_id
        if path.parent != self._root or not path.is_dir():
            raise SealedStoreError(f"no sealed data for task {task_id!r}")
        return path

    def check_script(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "check.sh"

    def hidden_tests_dir(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "hidden_tests"

    def hidden_test_paths(self, task_id: str) -> list[str]:
        """Paths of the hidden tests relative to the repository root, sorted."""
        root = self.hidden_tests_dir(task_id)
        if not root.is_dir():
            return []
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())

    def target_commit(self, task_id: str) -> str | None:
        path = self._task_dir(task_id) / "target_commit"
        return path.read_text(encoding="utf-8").strip() if path.exists() else None

    def reference_patch(self, task_id: str) -> str | None:
        path = self._task_dir(task_id) / "reference.patch"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def canary(self, task_id: str) -> str:
        value = (self._task_dir(task_id) / "canary.txt").read_text(encoding="utf-8").strip()
        if not value.startswith(CANARY_PREFIX):
            raise SealedStoreError(f"malformed canary for task {task_id!r}")
        return value

    def all_canaries(self) -> frozenset[str]:
        """Every known canary; the leak guard scans prompts for all of them."""
        if not self._root.is_dir():
            return frozenset()
        return frozenset(
            self.canary(p.name) for p in sorted(self._root.iterdir()) if (p / "canary.txt").exists()
        )

    def record_check_output(self, task_id: str, run_id: str, output: str) -> Path:
        """Store hidden-check output in the sealed store; it never enters a run record."""
        if not _RUN_ID.match(run_id):
            raise SealedStoreError(f"invalid run id: {run_id!r}")
        out_dir = self._task_dir(task_id) / "check_output"
        out_dir.mkdir(exist_ok=True, mode=0o700)
        path = out_dir / f"{run_id}.txt"
        path.write_text(output, encoding="utf-8")
        path.chmod(0o600)
        return path
