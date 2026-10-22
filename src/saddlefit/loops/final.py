"""Control runs for ``report --final``, and nothing else (B10, mechanism 5).

The only place in Saddlefit that asks ``BaselineRunner`` for the control split. It runs the
student on control tasks with the harness at a given commit, exactly as ``fit`` runs it on
other tasks, and returns per-task outcomes for ``saddlefit.eval.control`` to aggregate.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from saddlefit.config import Config
from saddlefit.eval.evaluator import Evaluator
from saddlefit.harness.loop_config import clamp, load_loop_config
from saddlefit.harness.store import HarnessStore
from saddlefit.loops.baseline import BaselineRunner, RunOutcome
from saddlefit.providers.base import ModelClient
from saddlefit.tasks.models import TaskMeta

CONTROL: Final = frozenset({"control"})


@dataclass
class ControlRunner:
    repo: Path
    config: Config
    store: HarnessStore
    public_dir: Path
    runs_root: Path
    suite_hash: str
    client_factory: Callable[[], ModelClient]  # leak-guarded by the caller
    evaluator: Evaluator
    docker_client: Any
    image: str
    tool_mode: Literal["native", "json"] = "native"
    on_run: Callable[[RunOutcome], None] | None = None

    def outcomes(self, ref: str, tasks: Sequence[TaskMeta]) -> tuple[dict[str, bool | None], float]:
        """Control outcomes per task under ``ref``, and the mean harness tokens per run."""
        control = [m for m in tasks if m.split == "control"]
        commit = self.store.head(ref)
        with tempfile.TemporaryDirectory(prefix="saddlefit-harness-") as tmp:
            harness_dir = Path(tmp)
            self.store.export(commit, harness_dir)
            loop, _ = clamp(load_loop_config(harness_dir / "loop.yaml"), self.config)
            result = BaselineRunner(
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
                splits=CONTROL,
            ).run(control)
        decided = result.solved()
        tokens = [r.harness_tokens for r in result.runs]
        mean = sum(tokens) / len(tokens) if tokens else 0.0
        return {m.id: decided.get(m.id) for m in control}, mean
