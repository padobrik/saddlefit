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
from saddlefit.eval.records import read_any_run
from saddlefit.harness.store import HarnessStore
from saddlefit.loops.baseline import RunOutcome
from saddlefit.loops.practice import DockerPractice, without_control
from saddlefit.providers.fake import FakeClient
from saddlefit.storage.runs import find_runs
from saddlefit.tasks import splits as splits_module
from saddlefit.tasks.mining import mine
from saddlefit.tasks.splits import new_lock
from saddlefit.tasks.suite import SuitePaths, materialize, save_lock

pytestmark = pytest.mark.docker


def test_practice_runs_batches_and_compares_held_out(
    tmp_path: Path, docker_client, sandbox_image: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a fixed salt that puts the eight toy tasks into all three splits (3 / 3 / 2)
    monkeypatch.setattr(splits_module.secrets, "token_hex", lambda n: "01" * 32)
    repo = build(tmp_path / "repo")
    config = Config(
        schema_version=3,
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
    train = [m.id for m in metas if m.split == "train"]
    held_out = [m.id for m in metas if m.split == "held_out"]
    control = [m.id for m in metas if m.split == "control"]
    assert (len(train), len(held_out), len(control)) == (3, 3, 2)
    assert {m.id for m in without_control(metas)} == set(train + held_out)

    store = HarnessStore(tmp_path / ".saddlefit" / "harness")
    store.init()
    ran: list[RunOutcome] = []
    practice = DockerPractice(
        repo=repo,
        config=config,
        store=store,
        tasks=metas,  # control included: DockerPractice must never run it
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

    for other in (held_out[:1], control[:1]):
        with pytest.raises(ValueError, match="not train tasks"):
            practice.run_batch("main", other)

    assert {r.task_id for r in ran} == set(train)  # on_run saw the training runs

    # the current harness is run once per commit; each comparison reruns only the candidate
    runs_root = tmp_path / ".saddlefit" / "runs"
    ran.clear()
    before = len(find_runs(runs_root))
    first = practice.compare_held_out("main", "main")
    assert (first.wins, first.losses, first.tasks) == (0, 0, len(held_out))
    assert len(find_runs(runs_root)) - before == 2 * len(held_out)
    before = len(find_runs(runs_root))
    practice.compare_held_out("main", "main")
    new = find_runs(runs_root)[before:]
    assert len(new) == len(held_out)
    # never a control task, and no held-out run reached the caller's callback
    assert {read_any_run(p)[1].task_id for p in new} == set(held_out)
    assert ran == []
