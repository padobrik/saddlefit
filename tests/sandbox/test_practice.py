"""``DockerPractice`` on the toy repository: real sandboxes and evaluator, a scripted student."""

import sys
from datetime import date
from pathlib import Path

import pytest
from tests.fixtures.toyrepo.build import build
from tests.integration.host_runner import HostRunner
from tests.sandbox.test_baseline import student

from saddlefit.config import Config, EvaluationConfig, SandboxConfig
from saddlefit.eval.evaluator import Evaluator
from saddlefit.eval.leak_guard import GuardedClient, guard_for_suite
from saddlefit.harness.store import HarnessStore
from saddlefit.loops.baseline import RunOutcome
from saddlefit.loops.practice import DockerPractice, without_control
from saddlefit.providers.fake import FakeClient
from saddlefit.tasks.mining import mine
from saddlefit.tasks.splits import new_lock
from saddlefit.tasks.suite import SuitePaths, materialize, save_lock

pytestmark = pytest.mark.docker


def test_practice_runs_batches_and_compares_held_out(
    tmp_path: Path, docker_client, sandbox_image: str
) -> None:
    repo = build(tmp_path / "repo")
    config = Config(
        schema_version=2,
        project="toy",
        evaluation=EvaluationConfig(runs_per_task=1, solved_threshold=1, timeout_seconds=120),
        sandbox=SandboxConfig(memory="512m", image=sandbox_image),
    )
    paths = SuitePaths(tmp_path / ".saddlefit" / "tasks")
    lock = new_lock(config.splits)
    metas = []
    for candidate in mine(repo, HostRunner(python=sys.executable)).viable:
        made = materialize(paths, repo, candidate, lock, created=date(2026, 10, 22))
        assert made is not None
        lock, meta = made
        metas.append(meta)
    save_lock(paths, lock)
    tasks = without_control(metas)
    train = [m.id for m in tasks if m.split == "train"]
    held_out = [m.id for m in tasks if m.split == "held_out"]
    control = [m.id for m in metas if m.split == "control"]
    assert train, "the toy suite needs training tasks for this test"

    store = HarnessStore(tmp_path / ".saddlefit" / "harness")
    store.init()
    ran: list[RunOutcome] = []
    practice = DockerPractice(
        repo=repo,
        config=config,
        store=store,
        tasks=tasks,
        public_dir=paths.public_dir,
        runs_root=tmp_path / ".saddlefit" / "runs",
        suite_hash=lock.suite_hash(),
        client_factory=lambda: GuardedClient(
            FakeClient(student), guard_for_suite(paths.sealed_dir)
        ),
        evaluator=Evaluator.for_suite(
            paths.sealed_dir, docker_client, sandbox_image, config.sandbox, config.evaluation
        ),
        docker_client=docker_client,
        image=sandbox_image,
        on_run=ran.append,
    )

    batch = practice.run_batch("main", train)
    assert set(batch.solved) == set(train)
    unsolved = [t for t, ok in batch.solved.items() if ok is False]
    assert len(batch.failures) == len(unsolved)
    for digest in batch.failures:
        assert digest.outcome == "fail"
        assert digest.task_text  # the task the student saw

    with pytest.raises(ValueError, match="not train tasks"):
        practice.run_batch("main", held_out[:1] or control[:1] or ["no-such-task"])

    # the current harness is run once per commit; each comparison reruns only the candidate
    ran.clear()
    first = practice.compare_held_out("main", "main")
    assert (first.wins, first.losses, first.tasks) == (0, 0, len(held_out))
    assert len(ran) == 2 * len(held_out)
    ran.clear()
    practice.compare_held_out("main", "main")
    assert len(ran) == len(held_out)
    assert not {r.task_id for r in ran} & set(control)
