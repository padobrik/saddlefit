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
from typing import Any, Literal

from saddlefit import __version__
from saddlefit.config import CONFIG_SCHEMA_VERSION, Config, StudentConfig, dump_config, load_config
from saddlefit.doctor import DoctorRecord
from saddlefit.errors import UserError
from saddlefit.eval.evaluator import Evaluator
from saddlefit.eval.leak_guard import GuardedClient, guard_for_suite
from saddlefit.eval.progress import harness_progress
from saddlefit.harness.lint import SandboxLinter
from saddlefit.harness.loop_config import LoopConfig, clamp, load_loop_config
from saddlefit.harness.store import HarnessStore, HarnessStoreError
from saddlefit.loops.ablation import AblationResult, AblationTarget, ablate, active_changes
from saddlefit.loops.baseline import (
    BASELINE_SPLITS,
    MAX_REPLY_TOKENS,
    BaselineRunner,
    RunOutcome,
)
from saddlefit.loops.fit import run_rounds
from saddlefit.loops.middle import MiddleLoop, RoundReport
from saddlefit.loops.practice import DockerPractice, without_control
from saddlefit.providers.base import ModelClient, ModelInfo
from saddlefit.providers.retry import LOCAL_TIMEOUT_S
from saddlefit.reports.baseline import ReportContext
from saddlefit.reports.baseline import build as build_report
from saddlefit.reports.baseline import write as write_report
from saddlefit.reports.progress import ProgressContext
from saddlefit.reports.progress import build as build_progress
from saddlefit.reports.progress import write as write_progress
from saddlefit.sandbox.environment import ensure_environment
from saddlefit.storage.fit_state import (
    ensure_unlocked,
    fit_lock,
    load_fit_state,
    save_fit_state,
)
from saddlefit.storage.library import ChangeRecord, read_records
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

    def environment_image(self, docker_client: Any) -> str:
        """The image this repository's tests run in: the sandbox plus its dependencies (B11).

        Built in its own step, which may use the network, and reused while the dependency
        files and the base image are unchanged. A repository without dependency files runs
        in the base image.
        """
        config = self.config()
        record = ensure_environment(
            docker_client, config.sandbox.image, self.root, config.project, config.sandbox
        )
        path = self.state / "environment.json"
        if record is None:
            path.unlink(missing_ok=True)  # an older record would name an image no longer used
            return config.sandbox.image
        path.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return record.image

    def student_timeout(self, digest: str | None = None) -> float:
        """Seconds one model call may take: a full reply at the measured speed, with room.

        The provider's local default (120 s) is too short for a 4,096-token reply from a
        model that writes about 24 tokens a second, so a long, careful reply would time out.
        ``saddlefit doctor`` measures the speed; without its record the default stays.
        """
        record = self._doctor_record(digest)
        if record is None or record.tokens_per_second <= 0:
            return LOCAL_TIMEOUT_S
        return max(LOCAL_TIMEOUT_S, MAX_REPLY_TOKENS / record.tokens_per_second * 1.5)

    def tool_mode(self, digest: str | None = None) -> Literal["native", "json"]:
        """The configured tool-calling mode; for ``auto``, the one ``doctor`` chose (B5)."""
        configured = self.config().student.tool_mode
        if configured != "auto":
            return configured
        record = self._doctor_record(digest)
        return record.tool_mode if record is not None else "native"

    def _doctor_record(self, digest: str | None = None) -> DoctorRecord | None:
        """``doctor.json`` when it was measured for this model, else ``None``.

        The record must name the configured model and, when both the record and the
        running model report a digest, the same digest: a name can move to new weights.
        """
        try:
            path = self.state / "doctor.json"
            record = DoctorRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if record.model != self.config().student.model:
            return None
        if digest is not None and record.digest is not None and record.digest != digest:
            return None
        return record

    def effective_loop(self) -> tuple[LoopConfig, list[str]]:
        """``loop.yaml`` from the harness store, clamped to ``saddlefit.yaml``."""
        return clamp(load_loop_config(self.harness.path / "loop.yaml"), self.config())

    def baseline(
        self,
        *,
        docker_client: Any,
        model_client: Callable[[], ModelClient],
        model_info: ModelInfo | None = None,
        limit: int | None = None,
        on_run: Callable[[RunOutcome], None] | None = None,
    ) -> tuple[dict[str, Any], Path]:
        """Run the baseline on training and held-out tasks and write its report.

        ``model_info`` is the model as the command saw it once; the tool mode and the
        report both use it, so they cannot disagree about which model ran.
        """
        config = self.config()
        info = model_info or model_client().info()
        loop, _ = self.effective_loop()
        lock = load_lock(self.suite, config.splits)
        metas = [m for m in self.tasks() if m.split in BASELINE_SPLITS][:limit]
        if not metas:
            raise UserError("no training or held-out tasks; run `saddlefit tasks mine` first")
        sealed = self.suite.sealed_dir
        commit = self.harness.head()
        image = self.environment_image(docker_client)
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
                    sealed, docker_client, image, config.sandbox, config.evaluation
                ),
                docker_client=docker_client,
                image=image,
                tool_mode=self.tool_mode(info.digest),
                on_run=on_run,
            )
            result = runner.run(metas)
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

    def fit(
        self,
        *,
        docker_client: Any,
        model_client: Callable[[], ModelClient],
        tutor_client: ModelClient,
        model_info: ModelInfo,
        rounds: int,
        on_run: Callable[[RunOutcome], None] | None = None,
        on_round: Callable[[RoundReport], None] | None = None,
    ) -> list[RoundReport]:
        """Run ``rounds`` more middle-loop rounds; control tasks are never run (B10).

        Rounds continue where the last ``fit`` stopped, so each one takes a new batch.
        """
        config = self.config()
        practice, train = self._practice(
            docker_client=docker_client,
            model_client=model_client,
            model_info=model_info,
            on_run=on_run,
        )
        guard = guard_for_suite(self.suite.sealed_dir, self.incidents)
        loop_config, _ = self.effective_loop()
        middle = MiddleLoop(
            store=self.harness,
            practice=practice,
            tutor=GuardedClient(tutor_client, guard),  # the tutor's prompts are scanned too
            linter=SandboxLinter(docker_client, config.sandbox.image, config.sandbox),
            root=self.state,
            train_tasks=train,
            student_model=model_info.model,
            student_digest=model_info.digest,
            context_budget=loop_config.context.budget_tokens,
            max_calls_per_round=config.tutor.max_calls_per_round,
            max_usd_per_round=config.tutor.max_usd_per_round,
            prices=config.tutor.prices_usd_per_mtok,
        )
        with fit_lock(self.state):
            return run_rounds(middle, self.state, rounds, on_round)

    def prune(
        self,
        *,
        docker_client: Any,
        model_client: Callable[[], ModelClient],
        model_info: ModelInfo,
        on_run: Callable[[RunOutcome], None] | None = None,
    ) -> list[AblationResult]:
        """Ablate now: test every accepted change and prune those that do not help (B12).

        Only the student runs, so no tutor and no API key are needed. Holds the fit lock and
        resets the count of accepted changes since the last ablation.
        """
        if self.nothing_to_prune():
            return []  # nothing to test, so no tasks, model or Docker are needed
        practice, train = self._practice(
            docker_client=docker_client,
            model_client=model_client,
            model_info=model_info,
            on_run=on_run,
        )
        target = AblationTarget(
            store=self.harness,
            practice=practice,
            root=self.state,
            train_tasks=train,
            student_model=model_info.model,
            student_digest=model_info.digest,
        )
        with fit_lock(self.state):
            results = ablate(target)
            state = load_fit_state(self.state)
            save_fit_state(self.state, state.model_copy(update={"accepted_since_ablation": 0}))
        return results

    def nothing_to_prune(self) -> bool:
        """True when no accepted change is left to test; an uninitialised project or a
        running fit is an error, not "nothing to do"."""
        self.config()  # raises with a pointer to `saddlefit init`
        ensure_unlocked(self.state)
        return not self.active_changes()

    def active_changes(self) -> list[ChangeRecord]:
        """Accepted changes not pruned since, oldest first."""
        return active_changes(read_records(self.state))

    def _practice(
        self,
        *,
        docker_client: Any,
        model_client: Callable[[], ModelClient],
        model_info: ModelInfo,
        on_run: Callable[[RunOutcome], None] | None,
    ) -> tuple[DockerPractice, list[str]]:
        """The student on training and held-out tasks, leak-guarded; never on control."""
        config = self.config()
        tasks = without_control(self.tasks())
        train = [m.id for m in tasks if m.split == "train"]
        if not train:
            raise UserError("no training tasks; run `saddlefit tasks mine` first")
        if not any(m.split == "held_out" for m in tasks):
            # no change could be judged on held-out tasks, so the work would be wasted
            raise UserError("no held-out tasks; mine more tasks first")
        sealed = self.suite.sealed_dir
        guard = guard_for_suite(sealed, self.incidents)
        image = self.environment_image(docker_client)
        practice = DockerPractice(
            repo=self.root,
            config=config,
            store=self.harness,
            tasks=tasks,
            public_dir=self.suite.public_dir,
            runs_root=self.runs,
            suite_hash=load_lock(self.suite, config.splits).suite_hash(),
            client_factory=lambda: GuardedClient(model_client(), guard),
            evaluator=Evaluator.for_suite(
                sealed, docker_client, image, config.sandbox, config.evaluation
            ),
            docker_client=docker_client,
            image=image,
            tool_mode=self.tool_mode(model_info.digest),
            on_run=on_run,
        )
        return practice, train

    def report(self) -> tuple[dict[str, Any], Path]:
        """Write a progress report: the baseline harness against the current one (B15)."""
        config = self.config()
        baseline = self.latest_report()
        before_tree = self.harness.tree(baseline["harness_commit"])
        if before_tree is None:
            raise UserError(
                f"the baseline's harness commit {baseline['harness_commit'][:12]} is not in the "
                "harness store; run `saddlefit baseline` again"
            )
        head = self.harness.head()
        after_tree = self.harness.tree(head)
        if after_tree is None:
            raise HarnessStoreError(f"main points to {head!r}, which has no tree")
        progress = harness_progress(
            self.runs,
            tree_of=self.harness.tree,
            before_tree=before_tree,
            after_tree=after_tree,
            task_types={m.id: m.type for m in self.tasks()},
            runs_per_task=config.evaluation.runs_per_task,
            threshold=config.evaluation.solved_threshold,
        )
        state = load_fit_state(self.state)
        ctx = ProgressContext(
            model=state.student_model or baseline["model"]["id"],
            model_digest=state.student_digest or baseline["model"]["digest"],
            baseline_harness_commit=baseline["harness_commit"],
            harness_commit=head,
            suite_hash=load_lock(self.suite, config.splits).suite_hash(),
            saddlefit_version=__version__,
            runs_per_task=config.evaluation.runs_per_task,
            solved_threshold=config.evaluation.solved_threshold,
            fit_rounds=state.rounds_done,
            tutor_calls=state.tutor_calls,
            tutor_usd=state.tutor_usd,
            control_evaluations=0,  # control runs only through `report --final`
        )
        report = build_progress(progress, ctx)
        return report, write_progress(report, self.reports, report_id("progress"))

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
