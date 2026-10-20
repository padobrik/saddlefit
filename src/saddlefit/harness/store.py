"""The harness store: ``.saddlefit/harness/`` as its own git repository (B3).

Every candidate change is a commit on ``candidate/<proposal-id>``; accepted changes are
merged into ``main``; pruned changes are reverted with the reason in the commit message.
Runs record the commit SHA they used.
"""

from __future__ import annotations

import os
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Final

from saddlefit.eval.tamper import diff_paths
from saddlefit.harness.allowlist import disallowed
from saddlefit.harness.loop_config import default_loop_config, dump_loop_config

MAIN: Final = "main"
_AUTHOR = ("saddlefit", "saddlefit@localhost.invalid")
_PROPOSAL_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")

DEFAULT_FILES: dict[str, str] = {
    "systemprompt.md": "You are a careful software engineer. Solve the task, then run its check.\n",
    "tool_guidance.md": "Prefer edit_file over write_file. Read before you edit.\n",
    "EXPERIENCE.md": "# Lessons\n",
    "skills/.gitkeep": "",
    "tools/.gitkeep": "",
}


class HarnessStoreError(Exception):
    """A git operation on the harness store failed or was refused."""


class HarnessStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _git(self, *args: str, stdin: str | None = None) -> str:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.path),
            # isolate from the user's git config, hooks and templates
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": _AUTHOR[0],
            "GIT_AUTHOR_EMAIL": _AUTHOR[1],
            "GIT_COMMITTER_NAME": _AUTHOR[0],
            "GIT_COMMITTER_EMAIL": _AUTHOR[1],
        }
        result = subprocess.run(  # noqa: S603 - fixed git argv, no shell
            ["git", "-c", "core.hooksPath=/dev/null", *args],  # noqa: S607
            cwd=self.path,
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise HarnessStoreError(f"git {args[0]} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def init(self) -> str:
        """Create the store with default harness files; return the initial commit SHA."""
        if (self.path / ".git").exists():
            raise HarnessStoreError(f"harness store already exists: {self.path}")
        self.path.mkdir(parents=True, exist_ok=True)
        self._git("init", "--quiet", f"--initial-branch={MAIN}")
        files = {**DEFAULT_FILES, "loop.yaml": dump_loop_config(default_loop_config())}
        for rel, content in files.items():
            target = self.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        self._git("add", "--all")
        self._git("commit", "--quiet", "-m", "harness: initial defaults")
        return self.head()

    def head(self, ref: str = MAIN) -> str:
        return self._git("rev-parse", ref)

    def propose(self, proposal_id: str, patch: str, message: str) -> str:
        """Commit ``patch`` on ``candidate/<id>`` from ``main``; return the candidate SHA."""
        if not _PROPOSAL_ID.match(proposal_id):
            raise HarnessStoreError(f"invalid proposal id: {proposal_id!r}")
        paths = diff_paths(patch)
        if not paths:
            raise HarnessStoreError("patch changes no files")
        refused = disallowed(paths)
        if refused:
            raise HarnessStoreError(f"patch touches paths outside the allowlist: {refused}")
        branch = f"candidate/{proposal_id}"
        self._git("switch", "--quiet", "-c", branch, MAIN)
        try:
            self._git("apply", "--check", "--whitespace=nowarn", "-", stdin=patch)
            self._git("apply", "--index", "--whitespace=nowarn", "-", stdin=patch)
            self._git("commit", "--quiet", "-m", f"{message}\n\nproposal: {proposal_id}")
            return self.head(branch)
        except HarnessStoreError:
            self._git("reset", "--quiet", "--hard")
            self._git("switch", "--quiet", MAIN)
            self._git("branch", "-D", branch)
            raise
        finally:
            self._git("switch", "--quiet", MAIN)

    def accept(self, proposal_id: str) -> str:
        """Merge the candidate into ``main``; return the new ``main`` SHA."""
        branch = f"candidate/{proposal_id}"
        self._git("merge", "--quiet", "--no-ff", "-m", f"accept {proposal_id}", branch)
        self._git("branch", "-d", branch)
        return self.head()

    def reject(self, proposal_id: str) -> None:
        self._git("branch", "-D", f"candidate/{proposal_id}")

    def prune(self, commit: str, reason: str) -> str:
        """Revert an accepted change on ``main``, recording why; return the new SHA."""
        self._git("revert", "--no-edit", "-m", "1", commit)
        self._git("commit", "--quiet", "--amend", "-m", f"prune {commit[:12]}\n\nreason: {reason}")
        return self.head()

    def tag(self, name: str, ref: str = MAIN) -> None:
        self._git("tag", name, ref)

    def export(self, ref: str, dest: Path) -> None:
        """Write the harness files at ``ref`` into ``dest`` (no .git)."""
        dest.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "harness.tar"
            self._git("archive", "--format=tar", f"--output={archive}", ref)
            with tarfile.open(archive) as tar:
                tar.extractall(dest, filter="data")
