"""A Saddlefit project: the ``.saddlefit/`` directory at a repository's root (B3).

The CLI is a thin layer over these operations: ``init``, mining tasks, listing them, the
baseline and its report.
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from saddlefit import __version__
from saddlefit.config import CONFIG_SCHEMA_VERSION, Config, StudentConfig, dump_config, load_config
from saddlefit.errors import EnvironmentFailure, UserError
from saddlefit.eval.evaluator import Evaluator
from saddlefit.eval.leak_guard import GuardedClient, guard_for_suite
from saddlefit.harness.loop_config import clamp, load_loop_config
from saddlefit.harness.store import HarnessStore
from saddlefit.loops.baseline import BASELINE_SPLITS, BaselineRunner, RunOutcome
from saddlefit.providers.base import ModelClient
from saddlefit.reports.baseline import ReportContext
from saddlefit.reports.baseline import build as build_report
from saddlefit.reports.baseline import write as write_report
from saddlefit.tasks.mining import LocalRunner, MiningReport, SandboxRunner, mine
from saddlefit.tasks.models import TaskMeta
from saddlefit.tasks.suite import SuitePaths, load_lock, load_meta, materialize, save_lock

STATE_DIR = ".saddlefit"


@dataclass(frozen=True)
class Project:
    root: Path

    @property
    def state(self) -> Path:
        return self.root / STATE_DIR

    @property
    def config_path(self) -> Path:
        return self.state / "saddlefit.yaml"

    @property
    def harness(self) -> HarnessStore:
        return HarnessStore(self.state / "harness")

    @property
    def suite(self) -> SuitePaths:
        return SuitePaths(self.state / "tasks")

    @property
    def runs(self) -> Path:
        return self.state / "runs"

    @property
    def reports(self) -> Path:
        return self.state / "reports"

    @property
    def incidents(self) -> Path:
        return self.state / "incidents.jsonl"

    def config(self) -> Config:
        return load_config(self.config_path)

    def init(self, model: str | None = None) -> dict[str, Any]:
        """Create ``.saddlefit/``: configuration, harness store and the split salt."""
        if not (self.root / ".git").exists():
            raise UserError(f"not a git repository: {self.root}")
        if self.config_path.exists():
            raise UserError(f"already initialised: {self.config_path}")
        self.state.mkdir(parents=True, exist_ok=True)
        student = StudentConfig(model=model) if model else StudentConfig()
        config = Config(
            schema_version=CONFIG_SCHEMA_VERSION, project=self.root.name, student=student
        )
        self.config_path.write_text(dump_config(config), encoding="utf-8")
        commit = self.harness.init()
        save_lock(self.suite, load_lock(self.suite, config.splits))
        self._ignore_state()
        return {
            "project": config.project,
            "model": config.student.model,
            "harness_commit": commit,
            "state": str(self.state),
        }

    def _ignore_state(self) -> None:
        gitignore = self.root / ".gitignore"
        lines = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
        if f"{STATE_DIR}/" not in lines:
            gitignore.write_text("\n".join([*lines, f"{STATE_DIR}/"]) + "\n", encoding="utf-8")

    def mine(
        self,
        *,
        docker_client: Any = None,
        limit: int | None = None,
        since: date | None = None,
        trust_local: bool = False,
    ) -> tuple[MiningReport, list[TaskMeta]]:
        """Mine history into the suite; candidates are tested in the sandbox unless trusted."""
        config = self.config()
        if trust_local:
            runner: Any = LocalRunner(python=sys.executable)
        else:
            if docker_client is None:
                raise EnvironmentFailure("Docker is needed to test candidates in the sandbox")
            runner = SandboxRunner(docker_client, config.sandbox.image, config.sandbox)
        report = mine(self.root, runner, since=since, limit=limit)
        lock = load_lock(self.suite, config.splits)
        metas = []
        for candidate in report.viable:
            made = materialize(self.suite, self.root, candidate, lock, created=date.today())
            if made is not None:
                lock, meta = made
                metas.append(meta)
        save_lock(self.suite, lock)
        return report, metas

    def tasks(self) -> list[TaskMeta]:
        lock = load_lock(self.suite, self.config().splits)
        return [load_meta(self.suite, t.id) for t in lock.tasks]

    def task_text(self, task_id: str) -> str:
        path = self.suite.public_dir / task_id / "task.md"
        if not path.exists():
            raise UserError(f"no such task: {task_id}")
        return path.read_text(encoding="utf-8")

    def effective_loop(self) -> tuple[Any, list[str]]:
        """``loop.yaml`` from the harness store, clamped to ``saddlefit.yaml``."""
        return clamp(load_loop_config(self.harness.path / "loop.yaml"), self.config())

    def baseline(
        self,
        *,
        docker_client: Any,
        model_client: Callable[[], ModelClient],
        limit: int | None = None,
        on_run: Callable[[RunOutcome], None] | None = None,
    ) -> tuple[dict[str, Any], Path]:
        """Run the baseline on training and held-out tasks and write its report."""
        config = self.config()
        loop, _ = self.effective_loop()
        lock = load_lock(self.suite, config.splits)
        metas = [m for m in self.tasks() if m.split in BASELINE_SPLITS][:limit]
        if not metas:
            raise UserError("no training or held-out tasks; run `saddlefit tasks mine` first")
        sealed = self.suite.sealed_dir
        commit = self.harness.head()
        with tempfile.TemporaryDirectory(prefix="saddlefit-harness-") as tmp:
            self.harness.export(commit, Path(tmp))
            runner = BaselineRunner(
                repo=self.root,
                config=config,
                loop=loop,
                harness_dir=Path(tmp),
                harness_commit=commit,
                suite_hash=lock.suite_hash(),
                public_dir=self.suite.public_dir,
                runs_root=self.runs,
                client_factory=lambda: GuardedClient(
                    model_client(), guard_for_suite(sealed, self.incidents)
                ),
                evaluator=Evaluator.for_suite(
                    sealed, docker_client, config.sandbox.image, config.sandbox, config.evaluation
                ),
                docker_client=docker_client,
                image=config.sandbox.image,
                on_run=on_run,
            )
            result = runner.run(metas)
        info = model_client().info()
        ctx = ReportContext(
            model=info.model,
            model_digest=info.digest,
            harness_commit=commit,
            suite_hash=lock.suite_hash(),
            saddlefit_version=__version__,
        )
        report = build_report(result, ctx)
        markdown, _ = write_report(report, self.reports, report_id("baseline"))
        return report, markdown

    def latest_report(self) -> dict[str, Any]:
        reports = sorted(self.reports.glob("baseline-*.json"))
        if not reports:
            raise UserError("no baseline report yet; run `saddlefit baseline`")
        loaded: dict[str, Any] = json.loads(reports[-1].read_text(encoding="utf-8"))
        return loaded


def report_id(kind: str) -> str:
    return f"{kind}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def version_info() -> str:
    return __version__
