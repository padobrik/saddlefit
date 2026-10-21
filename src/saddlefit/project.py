"""A Saddlefit project: the ``.saddlefit/`` directory at a repository's root (B3).

The CLI is a thin layer over these operations: ``init``, mining tasks and listing them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from saddlefit.config import CONFIG_SCHEMA_VERSION, Config, StudentConfig, dump_config, load_config
from saddlefit.errors import UserError
from saddlefit.harness.store import HarnessStore
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
        commit = self.harness.init()
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
