"""The whole baseline pipeline on the toy repository, with a scripted student."""

import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from tests.fixtures.toyrepo.build import build
from tests.integration.host_runner import HostRunner

from saddlefit.config import Config, EvaluationConfig, SandboxConfig
from saddlefit.eval.evaluator import Evaluator
from saddlefit.eval.leak_guard import GuardedClient, guard_for_suite
from saddlefit.harness.loop_config import default_loop_config
from saddlefit.harness.store import HarnessStore
from saddlefit.loops.baseline import BASELINE_SPLITS, BaselineRunner
from saddlefit.providers.base import ModelReply, ToolCall
from saddlefit.providers.fake import FakeClient
from saddlefit.storage.runs import find_runs, read_run
from saddlefit.tasks import splits as splits_module
from saddlefit.tasks.mining import mine
from saddlefit.tasks.splits import new_lock
from saddlefit.tasks.suite import SuitePaths, materialize, save_lock

pytestmark = pytest.mark.docker

FIX = ToolCall(
    id="c1",
    name="edit_file",
    arguments={
        "path": "src/toykit/dates.py",
        "old": 'year, day, month = text.split("-")',
        "new": 'year, month, day = text.split("-")',
    },
)


SEEN_REASONING: list[object] = []


def student(messages: Any, tools: Any, params: Any) -> ModelReply:
    """Fixes parse_iso when asked about it; everywhere else it gives up at once."""
    SEEN_REASONING.append(params.reasoning)
    task = messages[1].content
    already_edited = any(m.role == "tool" for m in messages)
    if "parse_iso" in task and not already_edited:
        return ModelReply(tool_calls=(FIX,))
    return ModelReply(tool_calls=(ToolCall(id="c2", name="finish", arguments={"summary": "x"}),))


def test_baseline_end_to_end(
    tmp_path: Path, docker_client, sandbox_image: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # every toy task goes to train or held-out: control is never run by a baseline
    monkeypatch.setattr(splits_module.secrets, "token_hex", lambda n: "11" * 32)
    repo = build(tmp_path / "repo")
    config = Config(
        schema_version=1,
        project="toy",
        evaluation=EvaluationConfig(runs_per_task=1, solved_threshold=1, timeout_seconds=120),
        sandbox=SandboxConfig(memory="512m", image=sandbox_image),
    )
    paths = SuitePaths(tmp_path / ".saddlefit" / "tasks")
    lock = new_lock(config.splits)
    report = mine(repo, HostRunner(python=sys.executable))
    metas = []
    for candidate in report.viable:
        made = materialize(paths, repo, candidate, lock, created=date(2026, 10, 21))
        assert made is not None
        lock, meta = made
        metas.append(meta)
    save_lock(paths, lock)
    assert len(metas) == 8

    harness = HarnessStore(tmp_path / ".saddlefit" / "harness")
    commit = harness.init()
    runner = BaselineRunner(
        repo=repo,
        config=config,
        loop=default_loop_config(),
        harness_dir=harness.path,
        harness_commit=commit,
        suite_hash=lock.suite_hash(),
        public_dir=paths.public_dir,
        runs_root=tmp_path / ".saddlefit" / "runs",
        client_factory=lambda: GuardedClient(
            FakeClient(student), guard_for_suite(paths.sealed_dir)
        ),
        evaluator=Evaluator.for_suite(
            paths.sealed_dir, docker_client, sandbox_image, config.sandbox, config.evaluation
        ),
        docker_client=docker_client,
        image=sandbox_image,
    )
    SEEN_REASONING.clear()
    result = runner.run(metas)
    # loop.yaml's reasoning effort reaches every model call (B6)
    assert SEEN_REASONING
    assert set(SEEN_REASONING) == {default_loop_config().reasoning.effort}
    expected = [m for m in metas if m.split in BASELINE_SPLITS]
    assert {r.task_id for r in result.runs} == {m.id for m in expected}
    solved = result.solved()
    parse_iso = next(m.id for m in metas if "parse-iso" in m.id)
    for task_id, ok in solved.items():
        assert ok is (task_id == parse_iso), task_id
    assert result.failure_profile().get("gave_up", 0) == len(expected) - (parse_iso in solved)
    # train run records are readable; held-out ones only through saddlefit.eval
    records = find_runs(tmp_path / ".saddlefit" / "runs")
    assert len(records) == len(expected)
    train = [r for r in result.runs if r.split == "train"]
    if train:
        path = next(p for p in records if p.stem == train[0].run_id)
        _, summary = read_run(path)
        assert summary.harness_commit == commit
