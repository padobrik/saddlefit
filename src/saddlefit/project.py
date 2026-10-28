"""A Saddlefit project: the ``.saddlefit/`` directory at a repository's root (B3).

The CLI is a thin layer over these operations: ``init``, mining tasks, listing them, the
baseline and its report.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal

from ulid import ULID

from saddlefit import __version__
from saddlefit.config import CONFIG_SCHEMA_VERSION, Config, StudentConfig, dump_config, load_config
from saddlefit.doctor import DoctorRecord
from saddlefit.errors import UserError
from saddlefit.eval.control import control_summary
from saddlefit.eval.evaluator import Evaluator, protected_patterns
from saddlefit.eval.leak_guard import GuardedClient, guard_for_suite
from saddlefit.eval.progress import Comparable, harness_progress
from saddlefit.eval.tamper import matches_any
from saddlefit.export.skill import export_skill
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
from saddlefit.loops.fast import FastLoop, FastResult
from saddlefit.loops.final import ControlRunner
from saddlefit.loops.fit import run_rounds
from saddlefit.loops.generate import (
    CALIBRATION_RUNS,
    CalibrationError,
    Generation,
    GenerationReport,
    NoCandidate,
)
from saddlefit.loops.generate import MAX_REPLY_TOKENS as GENERATOR_REPLY_TOKENS
from saddlefit.loops.middle import MiddleLoop, RoundReport
from saddlefit.loops.practice import DockerPractice, without_control
from saddlefit.loops.progress import FitProgress
from saddlefit.loops.work import WorkRunner, write_answer
from saddlefit.playbook import proposal_for
from saddlefit.providers.base import ModelClient, ModelInfo
from saddlefit.providers.retry import LOCAL_TIMEOUT_S
from saddlefit.reports.baseline import ReportContext
from saddlefit.reports.baseline import build as build_report
from saddlefit.reports.baseline import write as write_report
from saddlefit.reports.progress import ProgressContext
from saddlefit.reports.progress import build as build_progress
from saddlefit.reports.progress import write as write_progress
from saddlefit.sandbox.environment import ensure_environment
from saddlefit.storage.control_log import (
    ControlEvaluation,
    control_evaluations,
    log_control_evaluation,
)
from saddlefit.storage.fit_state import (
    FIT_LOCK_FILE,
    FitState,
    ensure_unlocked,
    fit_lock,
    load_fit_state,
    lock_owner,
    save_fit_state,
)
from saddlefit.storage.journal import JOURNAL_SCHEMA_VERSION, JournalEntry, task_ref, write_entry
from saddlefit.storage.library import ChangeRecord, read_records
from saddlefit.storage.progress import ProgressRecord, load_progress, process_alive
from saddlefit.tasks import generated as generated_files
from saddlefit.tasks.acceptance import FAILED_TESTS, Acceptance, accept, seal_generated
from saddlefit.tasks.generator import (
    MAX_EXAMPLES,
    TASK_KINDS,
    GeneratedTask,
    GeneratorRequest,
    GeneratorResult,
    generate,
    render_system,
    render_user,
    select_files,
    task_title,
)
from saddlefit.tasks.mining import STATE_DIR, MiningReport, Runner, _export, _git_bytes, mine
from saddlefit.tasks.models import TASK_SCHEMA_VERSION, RepoRef, TaskMeta
from saddlefit.tasks.mutation import (
    MUTATION_TRIES_PER_CALL,
    Mutation,
    commit_mutation,
    drop_mutation,
    issue_messages,
    pick,
    related_tests,
    write_issue,
)
from saddlefit.tasks.overlap import (
    RepeatGuard,
    load_repeat_index,
    save_repeat_index,
    symbols_between,
)
from saddlefit.tasks.splits import add_task, new_lock
from saddlefit.tasks.suite import SuitePaths, load_lock, load_meta, materialize, save_lock


def _check_live_model(baseline: dict[str, Any], model_info: ModelInfo) -> None:
    """Control must run on the baseline's model: same name, and same digest when both known."""
    digests = {model_info.digest, baseline["model"]["digest"]}
    if model_info.model != baseline["model"]["id"] or (None not in digests and len(digests) > 1):
        raise UserError(
            f"the student model is {model_info.model} ({model_info.digest or 'no digest'}), "
            f"not the baseline's {baseline['model']['id']}; run `saddlefit baseline` again"
        )


@dataclass(frozen=True)
class WorkResult:
    result: FastResult
    answer: Path  # the patch, possibly empty
    journal: Path | None  # the probation entry, when playbook variants were needed


@dataclass(frozen=True)
class TaskKit:
    """What every new task needs, whatever wrote it: the sealed store, judging, calibration."""

    sealed_root: Path
    guard: Any
    evaluator: Evaluator
    image: str
    calibrate: Callable[[str, GeneratedTask, str], int]
    keep: Callable[[str, GeneratedTask], None]
    exists: Callable[[str], bool]
    repeats: RepeatGuard  # what a new task must not repeat; keep() adds each kept one


@dataclass(frozen=True)
class ReportBasis:
    """What a report compares against: the latest baseline, checked as still comparable."""

    config: Config
    baseline: dict[str, Any]
    before_tree: str
    state: FitState
    suite_hash: str


def _refuse_a_repeat(repeats: RepeatGuard, mutation: Mutation) -> None:
    """A mutation in a function a task in the suite changes is no new task."""
    undone = symbols_between(mutation.path, mutation.mutated, mutation.original)
    if repeated := repeats.repeats(undone):
        raise NoCandidate(f"{mutation.path}:{mutation.site.line}: {repeated}")


def _mutated_reader(root: Path, head: str, pending: dict[str, Any]) -> Callable[[str], str | None]:
    """Old files for a mutation task's reference patch, which undoes the pending mutation."""

    def read(path: str) -> str | None:
        """The mutated text for the mutated file, the committed one for any other."""
        mutation = pending["mutation"]
        if path == mutation.path:
            return str(mutation.mutated)
        return generated_files.read_text(root, head, path)

    return read


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

    def generate_tasks(
        self,
        *,
        kind: str,
        count: int,
        docker_client: Any,
        generator_client: ModelClient,
        model_client: Callable[[], ModelClient],
        model_info: ModelInfo,
    ) -> GenerationReport:
        """Generate ``count`` kept tasks of ``kind``, within the generator's budget (B13)."""
        if kind not in TASK_KINDS:
            raise UserError(f"unknown task type {kind!r}; known: {', '.join(sorted(TASK_KINDS))}")
        config = self.config()
        protected = protected_patterns(config.evaluation)
        head = _git_bytes(self.root, "rev-parse", "HEAD").decode().strip()
        paths = generated_files.tracked_paths(self.root, head)
        files = generated_files.repo_files(self.root, head, paths, protected)
        if not files:
            raise UserError("no source files the generator could change")
        summary = generated_files.repo_summary(self.root, head, paths)
        train = [m for m in self.tasks() if m.split == "train"][:MAX_EXAMPLES]
        examples = [self.task_text(m.id) for m in train]
        kit = self._task_kit(docker_client, model_client, model_info, "generated", kind)
        # every title in the suite, then each candidate's as it is tried: none is to be repeated
        covered = [task_title(self.task_text(m.id)) for m in self.tasks()]
        read_head = partial(generated_files.read_text, self.root, head)

        def request(rng: Any) -> GeneratorRequest:
            return GeneratorRequest(
                kind=TASK_KINDS[kind],
                repo_summary=summary,
                files=select_files(files, rng),
                examples=examples,
                protected=protected,
                existing_paths=frozenset(paths),
                covered=tuple(covered),
            )

        def call(req: GeneratorRequest) -> GeneratorResult:
            result = generate(
                GuardedClient(generator_client, kit.guard), req, GENERATOR_REPLY_TOKENS
            )
            covered.append(task_title(result.task.task_md))
            return result

        with tempfile.TemporaryDirectory(prefix="saddlefit-base-") as tmp:
            base = Path(tmp) / "base"
            _export(self.root, head, base)
            generation = Generation(
                kind=kind,
                budget=config.generator,
                request=request,
                call=call,
                seal=lambda task_id, task: seal_generated(kit.sealed_root, task_id, task),
                accept=lambda task_id, task: accept(kit.evaluator, task_id, base, task),
                calibrate=lambda task_id, task: kit.calibrate(task_id, task, head),
                exists=kit.exists,
                duplicate_of=lambda task_id, task: kit.repeats.check(
                    task_id, task.reference_patch, read_head
                ),
                keep=kit.keep,
                discard=lambda task_id: generated_files.discard(self.suite, task_id),
                prompt_chars=lambda req: len(render_system(req)) + len(render_user(req)),
            )
            return generation.run(count)

    def mutate_tasks(
        self,
        *,
        count: int,
        docker_client: Any,
        generator_client: ModelClient,
        model_client: Callable[[], ModelClient],
        model_info: ModelInfo,
        runner: Runner,
    ) -> GenerationReport:
        """Inject ``count`` kept bugs into working code; the generator writes each up (A11)."""
        config = self.config()
        protected = protected_patterns(config.evaluation)
        head = _git_bytes(self.root, "rev-parse", "HEAD").decode().strip()
        paths = generated_files.tracked_paths(self.root, head)
        # the cheap path filters first: one git show per test file, not per tracked file
        test_paths = [p for p in paths if p.endswith(".py") and matches_any(p, protected)]
        tests = {
            p: t
            for p in test_paths
            if (t := generated_files.read_text(self.root, head, p)) is not None
        }
        sources = {
            f.path: f.content
            for f in generated_files.repo_files(self.root, head, paths, protected)
            if f.path.endswith(".py") and related_tests(f.path, tests)
        }
        if not sources:
            raise UserError("no Python source file has tests that import it")
        kit = self._task_kit(docker_client, model_client, model_info, "mutation", "bugfix")
        pending: dict[str, Any] = {}
        passing: dict[tuple[str, ...], bool] = {}

        with tempfile.TemporaryDirectory(prefix="saddlefit-mutate-") as tmp:
            base = Path(tmp) / "base"
            _export(self.root, head, base)

            def request(rng: Any) -> tuple[Mutation, list[str]]:
                path = rng.choice(sorted(sources))
                mutation = pick(path, sources[path], rng)
                if mutation is None:
                    raise NoCandidate(f"{path}: no site to mutate")
                related = related_tests(path, tests)
                key = tuple(related)
                if key not in passing:  # the tests must pass before the bug goes in
                    passing[key] = runner.run_tests(base, related) == 0
                if not passing[key]:
                    raise NoCandidate(f"{path}: its tests fail without any mutation")
                _refuse_a_repeat(kit.repeats, mutation)  # before the call: it costs no max_calls
                mutated = Path(tmp) / "mutated"
                if mutated.exists():
                    remove_tree(mutated)
                # no bytecode caches: a .pyc with the same mtime and size would hide the bug
                shutil.copytree(
                    base, mutated, symlinks=True, ignore=shutil.ignore_patterns("__pycache__")
                )
                (mutated / path).write_bytes(mutation.mutated.encode("utf-8"))
                if runner.run_tests(mutated, related) not in FAILED_TESTS:
                    raise NoCandidate(f"{path}:{mutation.site.line}: the tests still pass")
                return mutation, related

            def call(req: tuple[Mutation, list[str]]) -> GeneratorResult:
                mutation, related = req
                result = write_issue(
                    GuardedClient(generator_client, kit.guard),
                    mutation,
                    related,
                    {p: tests[p] for p in related},
                    # no task-specific check: it would name the hidden tests and print them
                    visible_check="",
                )
                pending["mutation"], pending["related"] = mutation, related
                return result

            def seal(task_id: str, task: GeneratedTask) -> None:
                mutation, related = pending["mutation"], pending["related"]
                pending[task_id] = commit_mutation(self.root, head, mutation, task_id, related)
                seal_generated(kit.sealed_root, task_id, task)

            def judge(task_id: str, task: GeneratedTask) -> Acceptance:
                # a fresh directory per attempt: a repeated task ID must not meet old files
                snapshot = Path(tempfile.mkdtemp(dir=tmp)) / "base"
                _export(self.root, pending[task_id], snapshot)
                return accept(kit.evaluator, task_id, snapshot, task)

            def discard(task_id: str) -> None:
                generated_files.discard(self.suite, task_id)
                if task_id in pending:
                    drop_mutation(self.root, task_id)

            return Generation(
                kind="bugfix",
                id_prefix="mut",
                budget=config.generator,
                request=request,
                call=call,
                seal=seal,
                accept=judge,
                calibrate=lambda task_id, task: kit.calibrate(task_id, task, pending[task_id]),
                exists=kit.exists,
                duplicate_of=lambda task_id, task: kit.repeats.check(
                    task_id, task.reference_patch, _mutated_reader(self.root, head, pending)
                ),
                keep=kit.keep,
                discard=discard,
                prompt_chars=lambda req: sum(
                    len(m.content) for m in issue_messages(req[0], req[1])
                ),
                max_attempts=config.generator.max_calls * MUTATION_TRIES_PER_CALL,
            ).run(count)

    def _task_kit(
        self,
        docker_client: Any,
        model_client: Callable[[], ModelClient],
        model_info: ModelInfo,
        source: Literal["generated", "mutation"],
        kind: str,
    ) -> TaskKit:
        """Sealing, judging, calibrating and keeping new tasks of one source and type."""
        config = self.config()
        sealed = self.suite.sealed_dir
        guard = guard_for_suite(sealed, self.incidents)
        image = self.environment_image(docker_client)
        evaluator = Evaluator.for_suite(
            sealed, docker_client, image, config.sandbox, config.evaluation
        )
        loop, _ = self.effective_loop()
        calibration = config.model_copy(
            update={
                "evaluation": config.evaluation.model_copy(
                    update={"runs_per_task": CALIBRATION_RUNS, "solved_threshold": 1}
                )
            }
        )

        def calibrate(task_id: str, task: GeneratedTask, base_commit: str) -> int:
            lock = add_task(
                load_lock(self.suite, config.splits),
                task_id,
                source,
                generated_files.content_hash(task),
            )
            meta = TaskMeta(
                schema_version=TASK_SCHEMA_VERSION,
                id=task_id,
                source=source,
                type=kind,
                split=next(t.split for t in lock.tasks if t.id == task_id),
                repo=RepoRef(base_commit=base_commit),
                visible_check=task.visible_check or None,
                created=date.today(),
            )
            generated_files.publish(self.suite, meta, task)
            commit = self.harness.head()
            with tempfile.TemporaryDirectory(prefix="saddlefit-harness-") as tmp:
                self.harness.export(commit, Path(tmp))
                runner = BaselineRunner(
                    repo=self.root,
                    config=calibration,
                    loop=loop,
                    harness_dir=Path(tmp),
                    harness_commit=commit,
                    suite_hash=lock.suite_hash(),
                    public_dir=self.suite.public_dir,
                    runs_root=self.state / "calibration",
                    client_factory=lambda: GuardedClient(model_client(), guard),
                    evaluator=evaluator,
                    docker_client=docker_client,
                    image=image,
                    tool_mode=self.tool_mode(model_info.digest),
                )
                result = runner.run([meta])
            counted = [run for run in result.runs if run.classification.counts_in_statistics]
            if len(counted) < CALIBRATION_RUNS:
                raise CalibrationError(
                    f"only {len(counted)} of {CALIBRATION_RUNS} calibration runs counted; "
                    "the others were environment errors"
                )
            return sum(run.passed for run in counted)

        in_suite = {meta.id for meta in self.tasks()}
        recorded = load_repeat_index(self.suite.repeats).tasks
        repeats = RepeatGuard(
            {task_id: frozenset(s) for task_id, s in recorded.items() if task_id in in_suite}
        )

        def keep(task_id: str, task: GeneratedTask) -> None:
            # the index first: if it cannot be written, the lock never names the task, so the
            # discard that follows leaves a consistent suite; an entry for a task the lock
            # does not name is ignored when the guard is built
            save_repeat_index(self.suite.repeats, repeats.index(adding=task_id))
            lock = load_lock(self.suite, config.splits)
            save_lock(
                self.suite, add_task(lock, task_id, source, generated_files.content_hash(task))
            )
            repeats.kept(task_id)  # later candidates of this command must not repeat it

        return TaskKit(
            sealed_root=sealed,
            guard=guard,
            evaluator=evaluator,
            image=image,
            calibrate=calibrate,
            keep=keep,
            exists=lambda task_id: any(t.id == task_id for t in self.tasks()),
            repeats=repeats,
        )

    def work(
        self,
        *,
        task_text: str,
        check: str,
        docker_client: Any,
        model_client: Callable[[], ModelClient],
        model_info: ModelInfo,
    ) -> WorkResult:
        """Do ``task_text`` in a copy of the working tree with the fast loop (A6, B14).

        The answer is a patch in ``.saddlefit/answers/``; the working tree is not touched.
        A rescue that needed playbook variants is journaled, on probation.
        """
        config = self.config()
        guard = guard_for_suite(self.suite.sealed_dir, self.incidents)
        image = self.environment_image(docker_client)
        loop, _ = self.effective_loop()
        commit = self.harness.head()
        with tempfile.TemporaryDirectory(prefix="saddlefit-harness-") as tmp:
            self.harness.export(commit, Path(tmp))
            runner = WorkRunner(
                repo=self.root,
                config=config,
                task_text=task_text,
                check=check,
                harness_dir=Path(tmp),
                state=self.state,
                client_factory=lambda: GuardedClient(model_client(), guard),
                docker_client=docker_client,
                image=image,
                tool_mode=self.tool_mode(model_info.digest),
            )
            result = FastLoop(
                budget=config.fast_loop, loop=loop, attempt=runner.attempt, seed=config.student.seed
            ).run()
            loop_yaml = (Path(tmp) / "loop.yaml").read_text(encoding="utf-8")
        answers = self.state / "answers"
        answers.mkdir(parents=True, exist_ok=True)
        answer = answers / f"{result.probes[-1].run_id}.patch"
        write_answer(answer, result.diff)
        entry = None
        if result.helped:
            proposal = proposal_for(result.helped, loop_yaml, loop, source="fast_loop")
            if proposal is not None:
                entry = write_entry(
                    self.state,
                    JournalEntry(
                        schema_version=JOURNAL_SCHEMA_VERSION,
                        entry_id=str(ULID()),
                        created=datetime.now(UTC),
                        task=task_ref(task_text, check),
                        proposal=proposal,
                        probe_run_ids=tuple(p.run_id for p in result.probes),
                        passed=result.passed,
                        status="probation" if result.passed else "rejected",
                    ),
                )
        return WorkResult(result=result, answer=answer, journal=entry)

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
        on_progress: Callable[[ProgressRecord], None] | None = None,
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
        progress = FitProgress(
            root=self.state,
            rounds=rounds,
            candidates=middle.candidates_per_round,
            on_change=on_progress,
        )
        practice.progress = progress
        with fit_lock(self.state):
            reports = run_rounds(middle, self.state, rounds, on_round, progress)
            # under the lock, so it never removes the record of a fit that starts next;
            # a fit that fails keeps progress.json, so status can say where it stopped
            progress.finish()
        return reports

    def status(self) -> dict[str, Any]:
        """Where a fit is, from progress.json and fit.json; never waits for the fit lock."""
        state = load_fit_state(self.state)
        totals = {
            "rounds_done": state.rounds_done,
            "tutor_calls": state.tutor_calls,
            "tutor_usd": round(state.tutor_usd, 4),
        }
        # prune and report --final hold the same lock: whoever has it, and whether it runs
        owner = lock_owner(self.state)
        lock: dict[str, Any] | None = None
        if owner is not None:
            lock = {"pid": owner, "alive": process_alive(owner)}
        elif (self.state / FIT_LOCK_FILE).exists():  # created, but its pid never written
            lock = {"pid": None, "alive": None}
        record = load_progress(self.state)
        if record is None:
            return {"state": "idle", "lock": lock, **totals}
        # a failed fit keeps progress.json but drops the lock, and its pid can be reused
        active = owner == record.pid and process_alive(record.pid)
        return {
            "state": "running" if active else "interrupted",
            "progress": record.model_dump(mode="json"),
            "lock": lock,
            **totals,
        }

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
        basis = self._report_basis()
        report = self._progress_report(
            basis, self.harness.head(), control_evaluations(self.state), self.tasks()
        )
        return report, write_progress(report, self.reports, report_id("progress"))

    def final_report(
        self,
        *,
        docker_client: Any,
        model_client: Callable[[], ModelClient],
        model_info: ModelInfo,
        on_run: Callable[[RunOutcome], None] | None = None,
    ) -> tuple[dict[str, Any], Path]:
        """Freeze the harness, evaluate control once, and report it (B10 mechanism 5, B15).

        The only path that runs control tasks. The current ``main`` is tagged
        ``fit-<yyyy-mm-dd>-<n>``; control runs under the baseline's harness and under the tag,
        and the evaluation is logged and counted. Holds the fit lock throughout.
        """
        basis = self._report_basis()  # refuse an incomparable baseline before running anything
        _check_live_model(basis.baseline, model_info)  # the model that will actually run
        control = [m for m in self.tasks() if m.split == "control"]
        if not control:
            raise UserError("no control tasks; mine more tasks before `report --final`")
        runner = self._control_runner(basis, docker_client, model_client, model_info, on_run)
        with fit_lock(self.state):
            # a fit, a new baseline or new tasks may have come while the image was built
            fresh = self._report_basis()
            # one snapshot of the suite for the whole report: mining takes no fit lock, so the
            # snapshot must be exactly the tasks of the suite lock the hash was read from
            snapshot = self.tasks()
            lock = load_lock(self.suite, fresh.config.splits)
            if lock.suite_hash() != fresh.suite_hash or {m.id for m in snapshot} != {
                t.id for t in lock.tasks
            }:
                raise UserError(
                    "the task suite changed while `report --final` was preparing; run it again"
                )
            fresh_control = [m.id for m in snapshot if m.split == "control"]
            # the runner was built from the old inputs: any change to them refuses the run
            if (fresh.config, fresh.baseline, fresh.suite_hash, fresh_control) != (
                basis.config,
                basis.baseline,
                basis.suite_hash,
                [m.id for m in control],
            ):
                raise UserError(
                    "the configuration, the task suite or the baseline changed while "
                    "`report --final` was preparing; run it again"
                )
            _check_live_model(fresh.baseline, model_info)
            basis = fresh
            frozen = self.harness.head()  # main cannot move while the fit lock is held
            baseline_commit = basis.baseline["harness_commit"]
            before, before_tokens = runner.outcomes(baseline_commit, control)
            if self.harness.tree(frozen) == basis.before_tree:
                after, tokens = before, before_tokens  # the same files: one run is enough
            else:
                after, tokens = runner.outcomes(frozen, control)
            # tagged only once control has run, so a failed evaluation leaves no tag
            tag = self.harness.freeze(date.today(), frozen)
            log = log_control_evaluation(
                self.state,
                ControlEvaluation(
                    tag=tag,
                    harness_commit=frozen,
                    baseline_harness_commit=baseline_commit,
                    created=datetime.now(UTC),
                    tasks=len(control),
                ),
            )
            report = self._progress_report(basis, frozen, len(log.evaluations), snapshot)
        report["kind"] = "final"
        report["frozen_tag"] = tag
        report["control"] = control_summary(
            before, after, task_types={m.id: m.type for m in control}, mean_harness_tokens=tokens
        )
        return report, write_progress(report, self.reports, report_id("final"))

    def _control_runner(
        self,
        basis: ReportBasis,
        docker_client: Any,
        model_client: Callable[[], ModelClient],
        model_info: ModelInfo,
        on_run: Callable[[RunOutcome], None] | None,
    ) -> ControlRunner:
        config = basis.config
        sealed = self.suite.sealed_dir
        guard = guard_for_suite(sealed, self.incidents)
        image = self.environment_image(docker_client)
        return ControlRunner(
            repo=self.root,
            config=config,
            store=self.harness,
            public_dir=self.suite.public_dir,
            runs_root=self.runs,
            suite_hash=basis.suite_hash,
            client_factory=lambda: GuardedClient(model_client(), guard),
            evaluator=Evaluator.for_suite(
                sealed, docker_client, image, config.sandbox, config.evaluation
            ),
            docker_client=docker_client,
            image=image,
            tool_mode=self.tool_mode(model_info.digest),
            on_run=on_run,
        )

    def export_skill(self, out: Path | None = None, ref: str = "main") -> list[Path]:
        """Export the harness at ``ref`` as Agent Skills (B14); return the skill folders."""
        config = self.config()
        state = load_fit_state(self.state)
        dest = out or self._new_export_dir()
        try:
            return export_skill(
                self.harness,
                ref,
                dest,
                project=config.project,
                model=state.student_model or config.student.model,
                guard=guard_for_suite(self.suite.sealed_dir, self.incidents),
            )
        except FileExistsError as exc:
            raise UserError(str(exc)) from exc
        except HarnessStoreError as exc:
            raise UserError(f"cannot export {ref!r}: {exc}") from exc

    def _new_export_dir(self) -> Path:
        """A fresh folder under ``exports/``; a random suffix keeps same-second exports apart."""
        stamp = report_id("skill")
        for _ in range(16):
            path = self.state / "exports" / f"{stamp}-{secrets.token_hex(3)}"
            if not path.exists():
                return path
        raise UserError("could not choose a free export folder; pass --out")

    def _report_basis(self) -> ReportBasis:
        """The latest baseline, checked to be comparable with the project as it is now."""
        config = self.config()
        baseline = self.latest_report()
        before_tree = self.harness.tree(baseline["harness_commit"])
        if before_tree is None:
            raise UserError(
                f"the baseline's harness commit {baseline['harness_commit'][:12]} is not in the "
                "harness store; run `saddlefit baseline` again"
            )
        state = load_fit_state(self.state)
        suite_hash = load_lock(self.suite, config.splits).suite_hash()
        self._check_comparable(baseline, config, state, suite_hash)
        return ReportBasis(config, baseline, before_tree, state, suite_hash)

    def _progress_report(
        self, basis: ReportBasis, head: str, control_count: int, metas: list[TaskMeta]
    ) -> dict[str, Any]:
        """Training and held-out aggregates for the harness at ``head`` against the baseline,
        over the task snapshot ``metas``."""
        config, baseline = basis.config, basis.baseline
        after_tree = self.harness.tree(head)
        if after_tree is None:
            raise HarnessStoreError(f"{head!r} has no tree in the harness store")
        progress = harness_progress(
            self.runs,
            tree_of=self.harness.tree,
            before_tree=basis.before_tree,
            after_tree=after_tree,
            tasks={s: [m.id for m in metas if m.split == s] for s in ("train", "held_out")},
            task_types={m.id: m.type for m in metas},
            comparable=Comparable(
                model=baseline["model"]["id"],
                digest=baseline["model"]["digest"],
                suite_hash=baseline["suite_hash"],
            ),
            runs_per_task=config.evaluation.runs_per_task,
            threshold=config.evaluation.solved_threshold,
        )
        ctx = ProgressContext(
            model=baseline["model"]["id"],
            model_digest=baseline["model"]["digest"],
            baseline_harness_commit=baseline["harness_commit"],
            harness_commit=head,
            suite_hash=basis.suite_hash,
            saddlefit_version=__version__,
            runs_per_task=config.evaluation.runs_per_task,
            solved_threshold=config.evaluation.solved_threshold,
            fit_rounds=basis.state.rounds_done,
            tutor_calls=basis.state.tutor_calls,
            tutor_usd=basis.state.tutor_usd,
            control_evaluations=control_count,
            tutor_refusals=basis.state.refusals,
        )
        return build_progress(progress, ctx)

    @staticmethod
    def _check_comparable(
        baseline: dict[str, Any], config: Config, state: FitState, suite_hash: str
    ) -> None:
        """Refuse a comparison where anything but the harness changed since the baseline."""
        settings = (config.evaluation.runs_per_task, config.evaluation.solved_threshold)
        if settings != (baseline["runs_per_task"], baseline["solved_threshold"]):
            raise UserError(
                "evaluation settings changed since the baseline (runs per task, solved "
                "threshold); run `saddlefit baseline` again"
            )
        if suite_hash != baseline["suite_hash"]:
            raise UserError(
                "the task suite changed since the baseline; run `saddlefit baseline` again"
            )
        model = state.student_model or config.student.model
        digests = {state.student_digest, baseline["model"]["digest"]}
        if model != baseline["model"]["id"] or (None not in digests and len(digests) > 1):
            raise UserError(
                "the student model changed since the baseline; run `saddlefit baseline` again"
            )

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
