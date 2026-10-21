"""A Saddlefit project: the ``.saddlefit/`` directory at a repository's root (B3).

The CLI is a thin layer over these operations: ``init``, mining tasks, listing them, the
baseline and its report.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from saddlefit import __version__
from saddlefit.config import CONFIG_SCHEMA_VERSION, Config, StudentConfig, dump_config, load_config
from saddlefit.errors import UserError
from saddlefit.eval.evaluator import Evaluator
from saddlefit.eval.leak_guard import GuardedClient, guard_for_suite
from saddlefit.harness.loop_config import LoopConfig, clamp, load_loop_config
from saddlefit.harness.store import HarnessStore, HarnessStoreError
from saddlefit.loops.baseline import BASELINE_SPLITS, BaselineRunner, RunOutcome
from saddlefit.providers.base import ModelClient
from saddlefit.reports.baseline import ReportContext
from saddlefit.reports.baseline import build as build_report
from saddlefit.reports.baseline import write as write_report
from saddlefit.tasks.mining import STATE_DIR, MiningReport, Runner, mine
from saddlefit.tasks.models import TaskMeta
from saddlefit.tasks.splits import new_lock
from saddlefit.tasks.suite import SuitePaths, load_lock, load_meta, materialize, save_lock


@dataclass(frozen=True)
class Project:
    """Paths and operations of one project; nothing is read until an operation needs it."""

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
        if not self.config_path.exists():
            raise UserError(f"not initialised: run `saddlefit init` in {self.root}")
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
        commit = self._harness_commit()
        if not self.suite.lock.exists():  # a retried init keeps the salt it already made
            save_lock(self.suite, new_lock(config.splits))
        self._ignore_state()
        # written last: an existing config marks a finished init, so a failed one can be retried
        self.config_path.write_text(dump_config(config), encoding="utf-8")
        return {
            "project": config.project,
            "model": config.student.model,
            "harness_commit": commit,
            "state": str(self.state),
        }

    def _harness_commit(self) -> str:
        """Create the harness store, or reuse one a failed init already finished.

        A store left without a first commit is incomplete, so it is built again.
        """
        if (self.harness.path / ".git").exists():
            try:
                return self.harness.head()
            except HarnessStoreError:
                if not self.harness.is_empty():
                    raise  # it holds commits: never delete it, report the git error
                remove_tree(self.harness.path)
        return self.harness.init()

    def _ignore_state(self) -> None:
        """B3: ``init`` adds ``.saddlefit/`` to ``.gitignore``."""
        gitignore = self.root / ".gitignore"
        lines = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
        if f"{STATE_DIR}/" not in lines:
            gitignore.write_text("\n".join([*lines, f"{STATE_DIR}/"]) + "\n", encoding="utf-8")

    def mine(
        self, runner: Runner, *, limit: int | None = None, since: date | None = None
    ) -> tuple[MiningReport, list[TaskMeta]]:
        """Mine history into the suite; ``runner`` decides where candidate tests run."""
        config = self.config()
        report = mine(self.root, runner, since=since, limit=limit)
        lock = load_lock(self.suite, config.splits)
        metas = []
        for candidate in report.viable:
            made = materialize(
                self.suite,
                self.root,
                candidate,
                lock,
                created=date.today(),
            )
            if made is not None:
                lock, meta = made
                metas.append(meta)
        save_lock(self.suite, lock)
        return report, metas

    def tasks(self) -> list[TaskMeta]:
        """Every task in ``suite.lock``, with its public metadata."""
        lock = load_lock(self.suite, self.config().splits)
        return [load_meta(self.suite, t.id) for t in lock.tasks]

    def task_text(self, task_id: str) -> str:
        """The public ``task.md``; the sealed part is never read here."""
        if task_id not in {meta.id for meta in self.tasks()}:
            raise UserError(f"no such task: {task_id}")
        return (self.suite.public_dir / task_id / "task.md").read_text(encoding="utf-8")

    def effective_loop(self) -> tuple[LoopConfig, list[str]]:
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
        """The newest baseline report's JSON."""
        reports = sorted(self.reports.glob("baseline-*.json"))
        if not reports:
            raise UserError("no baseline report yet; run `saddlefit baseline`")
        loaded: dict[str, Any] = json.loads(reports[-1].read_text(encoding="utf-8"))
        return loaded


def report_id(kind: str) -> str:
    """A sortable report name, ``<kind>-<UTC timestamp>``."""
    return f"{kind}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def remove_tree(path: Path) -> None:
    """Delete ``path`` even where git left read-only files, which Windows will not unlink."""

    def make_writable_and_retry(
        func: Callable[[str], object], target: str, exc: BaseException
    ) -> None:
        # only a refused read-only file is retried, and never through a symlink
        if not isinstance(exc, PermissionError) or os.path.islink(target):
            raise exc
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=make_writable_and_retry)
