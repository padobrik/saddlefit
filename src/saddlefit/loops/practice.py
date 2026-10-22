"""``Practice`` on the real student: Docker sandboxes, the evaluator and run records (A7).

Each call runs the student with the harness at a given commit, through the same
``BaselineRunner`` the baseline uses, on the same seeds every time. Training batches come
back per task, with their failed runs digested for the tutor; held-out tasks come back as
one aggregate per comparison. Control tasks are never run (B10).
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from saddlefit.config import Config
from saddlefit.eval.decisions import HeldOutComparison, compare_held_out
from saddlefit.eval.evaluator import Evaluator
from saddlefit.harness.loop_config import clamp, load_loop_config
from saddlefit.harness.store import HarnessStore
from saddlefit.loops.baseline import BaselineResult, BaselineRunner, RunOutcome
from saddlefit.loops.middle import BatchResult
from saddlefit.providers.base import ModelClient
from saddlefit.storage.runs import read_run
from saddlefit.tasks.models import TaskMeta
from saddlefit.tutor.diagnose import digest_run


@dataclass
class DockerPractice:
    repo: Path
    config: Config
    store: HarnessStore
    tasks: Sequence[TaskMeta]  # training and held-out tasks; control is filtered out
    public_dir: Path
    runs_root: Path
    suite_hash: str
    client_factory: Callable[[], ModelClient]  # leak-guarded by the caller
    evaluator: Evaluator
    docker_client: Any
    image: str
    tool_mode: Literal["native", "json"] = "native"
    on_run: Callable[[RunOutcome], None] | None = None
    _held_out: dict[str, dict[str, bool | None]] = field(default_factory=dict, repr=False)

    @property
    def runs_per_task(self) -> int:
        return self.config.evaluation.runs_per_task

    def _metas(self, split: str, ids: Sequence[str] | None = None) -> list[TaskMeta]:
        chosen = [m for m in self.tasks if m.split == split]
        if ids is None:
            return chosen
        by_id = {m.id: m for m in chosen}
        missing = sorted(set(ids) - set(by_id))
        if missing:
            raise ValueError(f"not {split} tasks: {missing}")
        return [by_id[i] for i in ids]

    def _run(self, ref: str, metas: Sequence[TaskMeta]) -> BaselineResult:
        """Run ``metas`` with the harness at ``ref``, its loop.yaml clamped to the config."""
        commit = self.store.head(ref)
        with tempfile.TemporaryDirectory(prefix="saddlefit-harness-") as tmp:
            harness_dir = Path(tmp)
            self.store.export(commit, harness_dir)
            loop, _ = clamp(load_loop_config(harness_dir / "loop.yaml"), self.config)
            runner = BaselineRunner(
                repo=self.repo,
                config=self.config,
                loop=loop,
                harness_dir=harness_dir,
                harness_commit=commit,
                suite_hash=self.suite_hash,
                public_dir=self.public_dir,
                runs_root=self.runs_root,
                client_factory=self.client_factory,
                evaluator=self.evaluator,
                docker_client=self.docker_client,
                image=self.image,
                tool_mode=self.tool_mode,
                on_run=self.on_run,
            )
            return runner.run(metas)

    def run_batch(self, harness_ref: str, task_ids: Sequence[str]) -> BatchResult:
        metas = self._metas("train", task_ids)
        result = self._run(harness_ref, metas)
        decided = result.solved()
        solved = {m.id: decided.get(m.id) for m in metas}
        texts = {
            m.id: (self.public_dir / m.id / "task.md").read_text(encoding="utf-8") for m in metas
        }
        failures = []
        for run in reversed(result.runs):  # newest first
            if run.passed or not run.classification.counts_in_statistics:
                continue
            path = next(self.runs_root.glob(f"*/{run.run_id}.jsonl"), None)
            if path is None:
                raise FileNotFoundError(f"run record {run.run_id} is missing")
            events, summary = read_run(path)  # refuses anything but training runs
            failures.append(digest_run(events, summary, texts[run.task_id]))
        return BatchResult(solved=solved, failures=tuple(failures))

    def compare_held_out(self, current_ref: str, candidate_ref: str) -> HeldOutComparison:
        metas = self._metas("held_out")
        current = self.store.head(current_ref)
        if current not in self._held_out:  # the current harness is run once per commit
            self._held_out[current] = self._outcomes(current, metas)
        candidate = self._outcomes(self.store.head(candidate_ref), metas)
        return compare_held_out(self._held_out[current], candidate)

    def _outcomes(self, commit: str, metas: Sequence[TaskMeta]) -> dict[str, bool | None]:
        decided = self._run(commit, metas).solved()
        return {m.id: decided.get(m.id) for m in metas}


def without_control(tasks: Sequence[TaskMeta]) -> list[TaskMeta]:
    """Training and held-out tasks only; ``fit`` never sees a control task (B10)."""
    return [m for m in tasks if m.split in {"train", "held_out"}]
