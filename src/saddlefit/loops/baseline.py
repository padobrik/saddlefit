"""The baseline: run the student on training and held-out tasks, never control (B14).

Each run starts from a fresh copy of the task's base snapshot in its own sandbox. The
student works behind the leak guard; the evaluator judges the resulting diff in a fresh
container; the failure taxonomy tags the run; a run record is written. A task is solved
when it passes in at least ``solved_threshold`` of ``runs_per_task`` runs (B12).
"""

from __future__ import annotations

import shutil
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from saddlefit.config import Config
from saddlefit.context.assembly import Harness, assemble
from saddlefit.eval.evaluator import Evaluator, Verdict
from saddlefit.eval.taxonomy import Classification, classify
from saddlefit.harness.loop_config import LoopConfig
from saddlefit.loops.run_record import run_signals, run_summary
from saddlefit.loops.workspace import WorkspaceTracker
from saddlefit.providers.base import GenParams, ModelClient
from saddlefit.sandbox.container import Sandbox, SandboxError
from saddlefit.storage.runs import (
    DiffStats,
    RunWriter,
    new_run_id,
)
from saddlefit.student.loop import StudentLoop, StudentResult
from saddlefit.tasks.mining import _export as export_tree
from saddlefit.tasks.models import TaskMeta
from saddlefit.tools.builtin import TOOL_NAMES, Toolbox

BASELINE_SPLITS: frozenset[str] = frozenset({"train", "held_out"})  # never control (B10)
ENV_ERROR_RETRIES = 1
# One reply, reasoning included; bounds the latency of a single model call.
MAX_REPLY_TOKENS = 4096


@dataclass(frozen=True)
class RunOutcome:
    task_id: str
    split: str
    run_id: str
    seed: int
    passed: bool
    classification: Classification
    harness_tokens: int


@dataclass
class BaselineResult:
    """Per-task outcomes and aggregates; control is never part of a baseline."""

    runs: list[RunOutcome] = field(default_factory=list)
    runs_per_task: int = 3
    solved_threshold: int = 2

    def solved(self) -> dict[str, bool]:
        passes: Counter[str] = Counter()
        seen: set[str] = set()
        for run in self.runs:
            seen.add(run.task_id)
            passes[run.task_id] += run.passed
        return {task: passes[task] >= self.solved_threshold for task in sorted(seen)}

    def by_split(self) -> dict[str, tuple[int, int]]:
        """split -> (solved tasks, tasks)."""
        split_of = {run.task_id: run.split for run in self.runs}
        totals: dict[str, list[int]] = {}
        for task, ok in self.solved().items():
            entry = totals.setdefault(split_of[task], [0, 0])
            entry[0] += ok
            entry[1] += 1
        return {split: (s, n) for split, (s, n) in sorted(totals.items())}

    def failure_profile(self) -> dict[str, int]:
        return dict(Counter(run.classification.primary for run in self.runs).most_common())


@dataclass
class _Recorder:
    writer: RunWriter

    def event(self, type: str, data: dict[str, object] | None = None) -> None:
        self.writer.event(type, data)  # type: ignore[arg-type]


@dataclass
class BaselineRunner:
    repo: Path
    config: Config
    loop: LoopConfig  # already clamped to saddlefit.yaml
    harness_dir: Path
    harness_commit: str
    suite_hash: str
    public_dir: Path
    runs_root: Path
    client_factory: Callable[[], ModelClient]  # leak-guarded by the caller
    evaluator: Evaluator
    docker_client: Any
    image: str
    tool_mode: Literal["native", "json"] = "native"
    clock: Callable[[], float] = time.monotonic
    on_run: Callable[[RunOutcome], None] | None = None

    def run(self, tasks: Sequence[TaskMeta]) -> BaselineResult:
        evaluation = self.config.evaluation
        result = BaselineResult(
            runs_per_task=evaluation.runs_per_task, solved_threshold=evaluation.solved_threshold
        )
        harness = Harness.load(self.harness_dir)
        for meta in tasks:
            if meta.split not in BASELINE_SPLITS:
                continue
            task_text = (self.public_dir / meta.id / "task.md").read_text(encoding="utf-8")
            with tempfile.TemporaryDirectory(prefix="saddlefit-base-") as tmp:
                base = Path(tmp) / "base"
                export_tree(self.repo, meta.repo.base_commit, base)
                for attempt in range(evaluation.runs_per_task):
                    seed = self.config.student.seed + attempt
                    outcome = self._run_with_retry(
                        meta=meta,
                        task_text=task_text,
                        harness=harness,
                        base=base,
                        attempt=attempt,
                        seed=seed,
                    )
                    result.runs.append(outcome)
                    if self.on_run is not None:
                        self.on_run(outcome)
        return result

    def _run_with_retry(
        self,
        *,
        meta: TaskMeta,
        task_text: str,
        harness: Harness,
        base: Path,
        attempt: int,
        seed: int,
    ) -> RunOutcome:
        outcome = self._run_once(
            meta=meta,
            task_text=task_text,
            harness=harness,
            base=base,
            attempt=attempt,
            seed=seed,
        )
        for _ in range(ENV_ERROR_RETRIES):
            if outcome.classification.counts_in_statistics:
                break
            outcome = self._run_once(
                meta=meta,
                task_text=task_text,
                harness=harness,
                base=base,
                attempt=attempt,
                seed=seed,
            )
        return outcome

    def _run_once(
        self,
        *,
        meta: TaskMeta,
        task_text: str,
        harness: Harness,
        base: Path,
        attempt: int,
        seed: int,
    ) -> RunOutcome:
        run_id = new_run_id()
        writer = RunWriter(self.runs_root, run_id)
        started = self.clock()
        context = assemble(
            harness,
            task_text=task_text,
            task_type=meta.type,
            model=self.config.student.model,
            enabled_tools=TOOL_NAMES,
            harness_budget_tokens=self.loop.context.harness_budget_tokens,
        )
        client = self.client_factory()
        student_result: StudentResult | None = None
        env_error = False
        diff = ""
        stats = DiffStats(files=0, insertions=0, deletions=0)
        with tempfile.TemporaryDirectory(prefix="saddlefit-run-") as tmp:
            work = Path(tmp) / "work"
            shutil.copytree(base, work, symlinks=True)
            tracker = WorkspaceTracker(work, Path(tmp) / "track.git")
            try:
                with Sandbox(
                    self.docker_client,
                    self.image,
                    work,
                    self.config.sandbox,
                    run_timeout_s=self.config.evaluation.timeout_seconds,
                    labels={"saddlefit.role": "student", "saddlefit.run": run_id},
                ) as box:
                    student = StudentLoop(
                        client=client,
                        tools=Toolbox(box, max_chars=self.loop.tool_output.max_chars),
                        loop=self.loop,
                        params=GenParams(
                            temperature=self.config.student.temperature,
                            seed=seed,
                            tool_mode=self.tool_mode,
                            max_tokens=MAX_REPLY_TOKENS,
                            reasoning=self.loop.reasoning.effort,
                        ),
                        recorder=_Recorder(writer),
                        visible_check=meta.visible_check,
                        time_limit_s=self.config.evaluation.timeout_seconds,
                        workspace_digest=tracker.digest,
                        clock=self.clock,
                    )
                    student_result = student.run(context)
                diff = tracker.diff()
                stats = tracker.diff_stats()
            except SandboxError as exc:
                env_error = True
                writer.event("error", {"kind": "sandbox", "message": str(exc)[:2000]})
        verdict = (
            Verdict(passed=False, exit_code=None, duration_s=0.0, env_error="sandbox")
            if env_error
            else self.evaluator.evaluate(meta.id, base, diff, run_id)
        )
        classification = classify(run_signals(student_result, verdict, stats, self.config))
        info = client.info()
        writer.finish(
            run_summary(
                run_id=run_id,
                meta=meta,
                attempt=attempt,
                seed=seed,
                info=info,
                runner=self,
                result=student_result,
                verdict=verdict,
                classification=classification,
                stats=stats,
                harness_tokens=context.harness_tokens,
                wall=self.clock() - started,
            )
        )
        return RunOutcome(
            task_id=meta.id,
            split=meta.split,
            run_id=run_id,
            seed=seed,
            passed=verdict.passed,
            classification=classification,
            harness_tokens=context.harness_tokens,
        )
