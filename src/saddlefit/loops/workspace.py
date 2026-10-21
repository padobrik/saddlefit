"""The student's workspace, tracked by a git directory that the sandbox never sees."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from saddlefit.storage.runs import DiffStats


class WorkspaceTracker:
    """Tracks the student's workspace with a git directory kept OUTSIDE the workspace.

    The workspace is mounted into the sandbox, so its own ``.git`` could carry hooks or a
    ``core.fsmonitor`` command planted by the student. This git directory is never
    mounted, and git runs with no global config and no hooks.
    """

    def __init__(self, work: Path, git_dir: Path) -> None:
        self.work = work
        self.git_dir = git_dir
        self._git("init", "--quiet")
        self._git("add", "-A")
        self._git("commit", "--quiet", "--allow-empty", "-m", "base")

    def _git(self, *args: str) -> str:
        """Git's output as text; bytes that are not UTF-8 survive as surrogate escapes."""
        return self._git_bytes(*args).decode("utf-8", errors="surrogateescape")

    def _git_bytes(self, *args: str) -> bytes:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.git_dir),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_DIR": str(self.git_dir),
            "GIT_WORK_TREE": str(self.work),
            "GIT_AUTHOR_NAME": "saddlefit",
            "GIT_AUTHOR_EMAIL": "saddlefit@localhost.invalid",
            "GIT_COMMITTER_NAME": "saddlefit",
            "GIT_COMMITTER_EMAIL": "saddlefit@localhost.invalid",
        }
        result = subprocess.run(  # noqa: S603 - fixed git argv
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args],  # noqa: S607
            cwd=self.work,
            env=env,
            capture_output=True,
            check=True,
        )
        # bytes, not text mode: text mode would fail on non-UTF-8 bytes and turn CRLF into LF
        return result.stdout

    def digest(self) -> str:
        """Changes as of now: tracked edits and new files, for loop detection."""
        self._git("add", "-A")
        return hashlib.sha256(self._git_bytes("diff", "--cached", "--binary")).hexdigest()

    def diff(self) -> str:
        self._git("add", "-A")
        return self._git("diff", "--cached", "--binary")

    def diff_stats(self) -> DiffStats:
        self._git("add", "-A")
        files = insertions = deletions = 0
        for line in self._git("diff", "--cached", "--numstat").splitlines():
            added, removed, _ = line.split("\t", 2)
            files += 1
            insertions += int(added) if added.isdigit() else 0
            deletions += int(removed) if removed.isdigit() else 0
        return DiffStats(files=files, insertions=insertions, deletions=deletions)
