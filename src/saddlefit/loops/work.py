"""Work mode: the student on the user's own task, judged by the user's own check (B14).

Each attempt copies the working tree (tracked files and untracked ones git does not
ignore, never ``.git`` or ``.saddlefit``) into a fresh sandbox, runs the student with the
check as its finish gate, then runs the check once more. The check is visible by design:
work mode has no hidden tests, so nothing here touches the sealed store. Each probe's events
go to ``.saddlefit/work/``: an event log, not a run record (it has no summary line, since a
work task belongs to no split), kept apart from the baseline's and fit's runs.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from saddlefit.config import Config
from saddlefit.context.assembly import Harness, assemble
from saddlefit.errors import EnvironmentFailure
from saddlefit.eval.evaluator import Verdict
from saddlefit.eval.taxonomy import classify
from saddlefit.harness.loop_config import LoopConfig
from saddlefit.loops.baseline import MAX_REPLY_TOKENS
from saddlefit.loops.fast import Probe
from saddlefit.loops.run_record import run_signals
from saddlefit.loops.workspace import WorkspaceTracker
from saddlefit.providers.base import GenParams, ModelClient
from saddlefit.sandbox.container import ExecResult, Sandbox, SandboxError
from saddlefit.storage.runs import RunWriter, new_run_id
from saddlefit.student.loop import StudentLoop
from saddlefit.tasks.mining import _git_env
from saddlefit.tools.builtin import Toolbox, specs_for

WORK_DIR: Final = "work"
SKIPPED: Final = (".git", ".saddlefit")
# of each run's wall clock; the final check has the rest, under the same deadline
STUDENT_SHARE: Final = 0.75


def working_tree_files(repo: Path) -> list[str]:
    """Tracked files, and untracked ones git does not ignore; what the user sees."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],  # noqa: S607
        cwd=repo,
        env=_git_env(),
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    return sorted(p for p in set(out.split("\0")) if p and p.split("/", 1)[0] not in SKIPPED)


def _inside(repo: Path, rel: str) -> bool:
    """No component of ``rel`` is a symlink or a Windows junction: the file is really here."""
    path = repo
    for part in Path(rel).parts:
        path = path / part
        if path.is_symlink() or path.is_junction():
            return False
    return True


def copy_working_tree(repo: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for rel in working_tree_files(repo):
        source = repo / rel
        # deleted in the working tree, or reached through a link that could point outside
        if not _inside(repo, rel) or not source.is_file():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def write_answer(path: Path, diff: str) -> None:
    """Write the answer patch byte for byte: a non-UTF-8 file's bytes come back from the
    tracker as surrogate escapes, and must leave the same way."""
    path.write_bytes(diff.encode("utf-8", "surrogateescape"))


@dataclass
class _Capped:
    """The sandbox as the student's tools see it: no command runs past the student's share,
    so the final check keeps the rest of the run."""

    box: Sandbox
    deadline: float  # on time.monotonic

    def exec(self, command: str, timeout_s: float) -> ExecResult:
        left = self.deadline - time.monotonic()
        if left <= 0:
            return ExecResult(exit_code=124, output="[timed out]", timed_out=True, duration_s=0.0)
        return self.box.exec(command, min(timeout_s, left))


@dataclass
class _Recorder:
    writer: RunWriter

    def event(self, type: str, data: dict[str, object] | None = None) -> None:
        self.writer.event(type, data)  # type: ignore[arg-type]


@dataclass
class WorkRunner:
    """Runs the student once on the task with the given loop settings."""

    repo: Path
    config: Config
    task_text: str
    check: str
    harness_dir: Path
    state: Path
    client_factory: Callable[[], ModelClient]
    docker_client: Any
    image: str
    tool_mode: Literal["native", "json"] = "native"

    def attempt(self, loop: LoopConfig, seed: int, deadline: float | None = None) -> Probe:
        """One run, within one wall-clock deadline (B11): ``evaluation.timeout_seconds``, cut
        to what is left before ``deadline`` (on ``time.monotonic``) once the workspace is
        ready, so setup counts too. The student gets ``STUDENT_SHARE`` of it, and the final
        check always has the rest inside the same deadline.
        """
        run_id = new_run_id()
        writer = RunWriter(self.state / WORK_DIR, run_id)
        harness = Harness.load(self.harness_dir)
        enabled = frozenset({*loop.tools.enabled, "finish"})
        tools_dir = self.harness_dir / "tools"
        scripts = (
            sorted(p for p in tools_dir.iterdir() if p.is_file()) if tools_dir.is_dir() else []
        )
        context = assemble(
            harness,
            task_text=self.task_text,
            task_type="change",
            model=self.config.student.model,
            enabled_tools=enabled,
            harness_budget_tokens=loop.context.harness_budget_tokens,
            scripts=[p.name for p in scripts],
        )
        # no protected-files rule: work mode has no hidden tests, and the user may want tests
        with tempfile.TemporaryDirectory(prefix="saddlefit-work-") as tmp:
            work = Path(tmp) / "work"
            copy_working_tree(self.repo, work)
            tracker = WorkspaceTracker(work, Path(tmp) / "track.git")
            run_s = float(self.config.evaluation.timeout_seconds)
            if deadline is not None:
                run_s = min(run_s, deadline - time.monotonic())
            if run_s <= 0:
                return Probe(run_id, False, ("timeout",), "")  # nothing left to run in
            result = None
            passed = False
            exit_code: int | None = None
            try:
                with Sandbox(
                    self.docker_client,
                    self.image,
                    work,
                    self.config.sandbox,
                    run_timeout_s=run_s,
                    labels={"saddlefit.role": "work", "saddlefit.run": run_id},
                    harness_tools=tools_dir if scripts else None,
                ) as box:
                    if deadline is not None:  # container start-up counts against the budget
                        box.limit_to(deadline)
                    if box.remaining_s <= 0:
                        return Probe(run_id, False, ("timeout",), "")
                    student_s = box.remaining_s * STUDENT_SHARE
                    student = StudentLoop(
                        client=self.client_factory(),
                        tools=Toolbox(
                            _Capped(box, time.monotonic() + student_s),
                            max_chars=loop.tool_output.max_chars,
                            enabled=enabled,
                            line_numbers=loop.tools.read_line_numbers,
                            strip_line_numbers=loop.tools.edit_strips_line_numbers,
                        ),
                        tool_specs=specs_for(enabled),
                        loop=loop,
                        params=GenParams(
                            temperature=self.config.student.temperature,
                            seed=seed,
                            tool_mode=self.tool_mode,
                            max_tokens=MAX_REPLY_TOKENS,
                            reasoning=loop.reasoning.effort,
                        ),
                        recorder=_Recorder(writer),
                        visible_check=self.check,
                        time_limit_s=student_s,
                        workspace_digest=tracker.digest,
                    )
                    result = student.run(context)
                    if result.stop_reason == "provider_error":
                        # the model runner failed: no check, the student did not really work
                        raise EnvironmentFailure(
                            "the student's model runner failed during the attempt"
                        )
                    # the check runs in what the student's share left of the same deadline
                    verdict_run = box.exec(self.check, run_s)  # bounded by the deadline too
                    exit_code = verdict_run.exit_code
                    # a check that ran out of time did not pass, whatever it exited with
                    passed = verdict_run.exit_code == 0 and not verdict_run.timed_out
                diff = tracker.diff()
                stats = tracker.diff_stats()
            except SandboxError as exc:
                writer.event("error", {"kind": "sandbox", "message": str(exc)[:2000]})
                # the check never ran: an environment failure (exit 3), not a failed task
                raise EnvironmentFailure(f"the sandbox failed: {exc}") from exc
        verdict = Verdict(passed=passed, exit_code=exit_code, duration_s=0.0)
        tags = classify(run_signals(result, verdict, stats, self.config)).tags
        return Probe(run_id, passed, tuple(tags), diff)
