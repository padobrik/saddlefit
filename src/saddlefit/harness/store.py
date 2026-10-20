"""The harness store: ``.saddlefit/harness/`` as its own git repository (B3).

Every candidate change is a commit on ``candidate/<proposal-id>``; accepted changes are
merged into ``main``; pruned changes are reverted with the reason in the commit message.
Runs record the commit SHA they used.

``harness.json`` at the store root records the layout's integer ``schema_version`` (model
``HarnessStoreInfo``, schema ``schemas/harness-store.schema.json``). It is outside the
proposal allowlist, and every operation refuses a store whose version it cannot read.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from saddlefit.eval.tamper import diff_paths
from saddlefit.harness.allowlist import disallowed
from saddlefit.harness.loop_config import default_loop_config, dump_loop_config

MAIN: Final = "main"
HARNESS_SCHEMA_VERSION: Final = 1
INFO_FILE: Final = "harness.json"
_AUTHOR = ("saddlefit", "saddlefit@localhost.invalid")
# Every name that reaches git's argv starts with a letter or digit, so none reads as an option.
_PROPOSAL_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,63}")
_REF = re.compile(r"[0-9A-Za-z][0-9A-Za-z._/^~-]{0,127}")  # names and revisions like main~1
_COMMIT = re.compile(r"[0-9a-f]{7,64}")
# Regular files only: a symlink could point a skill at anything on the host.
_MODE_LINE = re.compile(r"^(?:new file mode|new mode) (\d+)$", re.MULTILINE)
_FILE_MODES: Final = frozenset({"100644", "100755"})

DEFAULT_FILES: dict[str, str] = {
    "systemprompt.md": "You are a careful software engineer. Solve the task, then run its check.\n",
    "tool_guidance.md": "Prefer edit_file over write_file. Read before you edit.\n",
    "EXPERIENCE.md": "# Lessons\n",
    "skills/.gitkeep": "",
    "tools/.gitkeep": "",
}


class HarnessStoreError(Exception):
    """A git operation on the harness store failed or was refused."""


class HarnessStoreInfo(BaseModel):
    """``harness.json``: the version of the harness-store layout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]


def _checked(pattern: re.Pattern[str], value: str, what: str) -> str:
    if not pattern.fullmatch(value):
        raise HarnessStoreError(f"invalid {what}: {value!r}")
    return value


class HarnessStore:
    """A git repository of harness files; every change is a commit (B3)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _git(self, *args: str, stdin: str | None = None, index: Path | None = None) -> str:
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
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        # bytes, not text mode: on Windows text mode would turn a patch's \n into \r\n
        result = subprocess.run(  # noqa: S603 - fixed git argv, no shell
            ["git", "-c", "core.hooksPath=/dev/null", *args],  # noqa: S607
            cwd=self.path,
            env=env,
            input=None if stdin is None else stdin.encode("utf-8"),
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise HarnessStoreError(f"git {args[0]} failed: {stderr}")
        return result.stdout.decode("utf-8", errors="replace").strip()

    def init(self) -> str:
        """Create the store with default harness files; return the initial commit SHA."""
        if (self.path / ".git").exists():
            raise HarnessStoreError(f"harness store already exists: {self.path}")
        self.path.mkdir(parents=True, exist_ok=True)
        self._git("init", "--quiet", f"--initial-branch={MAIN}")
        info = HarnessStoreInfo(schema_version=HARNESS_SCHEMA_VERSION)
        files = {
            **DEFAULT_FILES,
            "loop.yaml": dump_loop_config(default_loop_config()),
            INFO_FILE: info.model_dump_json() + "\n",
        }
        for rel, content in files.items():
            target = self.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        self._git("add", "--all")
        self._git("commit", "--quiet", "-m", "harness: initial defaults")
        return self.head()

    def _check_format(self, ref: str = MAIN) -> None:
        """Refuse a store whose ``harness.json`` at ``ref`` this version cannot read."""
        try:
            raw = self._git("show", f"{ref}:{INFO_FILE}")
        except HarnessStoreError:
            raw = "{}"  # missing: reported as a missing version below
        try:
            info = json.loads(raw)
        except ValueError as exc:
            raise HarnessStoreError(f"unreadable {INFO_FILE} at {ref}") from exc
        version = info.get("schema_version") if isinstance(info, dict) else None
        # bool is an int subclass, so test the exact type
        if type(version) is not int:
            raise HarnessStoreError(f"{INFO_FILE} at {ref}: missing or non-integer schema_version")
        if version != HARNESS_SCHEMA_VERSION:
            raise HarnessStoreError(
                f"{INFO_FILE} at {ref}: schema_version {version} is not supported "
                f"(current {HARNESS_SCHEMA_VERSION})"
            )

    def head(self, ref: str = MAIN) -> str:
        """The commit SHA that ``ref`` points to."""
        return self._git("rev-parse", "--verify", "--quiet", _checked(_REF, ref, "ref"))

    def propose(self, proposal_id: str, patch: str, message: str) -> str:
        """Commit ``patch`` on ``candidate/<id>`` from ``main``; return the candidate SHA."""
        _checked(_PROPOSAL_ID, proposal_id, "proposal id")
        modes = set(_MODE_LINE.findall(patch)) - _FILE_MODES
        if modes:
            raise HarnessStoreError(f"patch adds a symlink or other special file (modes {modes})")
        paths = diff_paths(patch)
        if not paths:
            raise HarnessStoreError("patch changes no files")
        refused = disallowed(paths)
        if refused:
            raise HarnessStoreError(f"patch touches paths outside the allowlist: {refused}")
        self._check_format()
        branch = f"candidate/{proposal_id}"
        self._git("switch", "--quiet", "-c", branch, MAIN)
        try:
            self._git("apply", "--check", "--whitespace=nowarn", "-", stdin=patch)
            self._git("apply", "--index", "--whitespace=nowarn", "-", stdin=patch)
            self._git("commit", "--quiet", "-m", f"{message}\n\nproposal: {proposal_id}")
            sha = self.head(branch)
        except HarnessStoreError:
            # best effort: a failing cleanup must not replace the error that matters
            with contextlib.suppress(HarnessStoreError):
                self._git("reset", "--quiet", "--hard")
                self._git("switch", "--quiet", MAIN)
                self._git("branch", "-D", branch)
            raise
        self._git("switch", "--quiet", MAIN)
        return sha

    def accept(self, proposal_id: str) -> str:
        """Merge the candidate into ``main``; return the new ``main`` SHA."""
        branch = f"candidate/{_checked(_PROPOSAL_ID, proposal_id, 'proposal id')}"
        self._check_format()
        self._git("merge", "--quiet", "--no-ff", "-m", f"accept {proposal_id}", branch)
        self._git("branch", "-d", branch)
        return self.head()

    def reject(self, proposal_id: str) -> None:
        """Drop the candidate branch; ``main`` is untouched."""
        self._git("branch", "-D", f"candidate/{_checked(_PROPOSAL_ID, proposal_id, 'proposal id')}")

    def prune(self, commit: str, reason: str) -> str:
        """Revert an accepted change on ``main``, recording why; return the new SHA."""
        _checked(_COMMIT, commit, "commit")
        self._check_format()
        try:
            self._git("revert", "--no-commit", "-m", "1", commit)
        except HarnessStoreError:
            # a conflicting revert must not leave the store mid-revert
            with contextlib.suppress(HarnessStoreError):
                self._git("revert", "--abort")
            raise
        self._git("commit", "--quiet", "-m", f"prune {commit[:12]}\n\nreason: {reason}")
        return self.head()

    def tag(self, name: str, ref: str = MAIN) -> None:
        """Tag ``ref``, for example the harness a release was measured with."""
        self._git("tag", _checked(_REF, name, "tag name"), _checked(_REF, ref, "ref"))

    def export(self, ref: str, dest: Path) -> None:
        """Write the harness files at ``ref`` into ``dest`` (no .git).

        A temporary index is checked out instead of ``git archive``, which would honour
        ``export-ignore`` and let a committed ``.gitattributes`` hide files.
        """
        _checked(_REF, ref, "ref")
        self._check_format(ref)
        # git runs in the store, so a relative prefix would land inside it
        dest = dest.resolve()
        dest.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="saddlefit-harness-index-") as tmp:
            index = Path(tmp) / "index"
            self._git("read-tree", ref, index=index)
            self._git("checkout-index", "--all", f"--prefix={dest}{os.sep}", index=index)
