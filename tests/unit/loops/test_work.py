"""Work mode: one deadline per run, and the final check always keeps a share of it (B11)."""

import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from saddlefit.config import Config, EvaluationConfig
from saddlefit.errors import EnvironmentFailure
from saddlefit.harness.loop_config import default_loop_config
from saddlefit.harness.store import HarnessStore
from saddlefit.loops import work as work_module
from saddlefit.loops.work import STUDENT_SHARE, WorkRunner
from saddlefit.providers.base import ModelReply, ProviderError, ToolCall
from saddlefit.providers.fake import FakeClient
from saddlefit.sandbox.container import ExecResult, SandboxError
from saddlefit.tasks.mining import _git_env

TIMEOUT = 60


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0


class FakeBox:
    """A sandbox with the real deadline rule, on a clock the test moves."""

    def __init__(self, clock: Clock, run_timeout_s: float) -> None:
        self.clock = clock
        self.deadline = clock.now + run_timeout_s
        self.checks: list[float] = []  # the time each command was allowed

    def __enter__(self) -> "FakeBox":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.deadline - self.clock.now)

    def limit_to(self, deadline: float) -> None:
        self.deadline = min(self.deadline, deadline)

    def exec(self, command: str, timeout_s: float) -> ExecResult:
        left = max(0.0, self.deadline - self.clock.now)
        self.checks.append(min(timeout_s, left))  # what Sandbox.exec really allows
        if left <= 0:
            return ExecResult(exit_code=124, output="[timed out]", timed_out=True, duration_s=0)
        return ExecResult(exit_code=0, output="ok", timed_out=False, duration_s=0)


def test_the_final_check_keeps_its_share_of_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=_git_env())
    store = HarnessStore(tmp_path / "harness")
    store.init()
    clock = Clock()
    boxes: list[FakeBox] = []

    def sandbox(*args: Any, run_timeout_s: float, **kwargs: Any) -> FakeBox:
        boxes.append(FakeBox(clock, run_timeout_s))
        return boxes[-1]

    monkeypatch.setattr(work_module, "Sandbox", sandbox)

    def student(messages: Any, tools: Any, params: Any) -> ModelReply:
        clock.now += TIMEOUT * STUDENT_SHARE + 1  # the student overruns its own share
        return ModelReply(tool_calls=(ToolCall(id="f", name="finish", arguments={"summary": "x"}),))

    config = Config(
        schema_version=3, project="p", evaluation=EvaluationConfig(timeout_seconds=TIMEOUT)
    )
    loop = default_loop_config().model_copy(
        update={"finish_gate": default_loop_config().finish_gate.model_copy(update={"run": None})}
    )
    runner = WorkRunner(
        repo=repo,
        config=config,
        task_text="Do nothing.",
        check="true",
        harness_dir=store.path,
        state=tmp_path / "state",
        client_factory=lambda: FakeClient(student),
        docker_client=object(),
        image="img",
    )
    probe = runner.attempt(loop, seed=7)
    # one deadline for the whole run, and the check still has the rest of it
    assert boxes[0].deadline == 1000.0 + TIMEOUT
    assert boxes[0].checks[-1] == pytest.approx(TIMEOUT * (1 - STUDENT_SHARE) - 1)
    assert probe.passed


def test_an_attempt_ends_by_the_deadline_it_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boxes: list[float] = []

    def sandbox(*args: Any, run_timeout_s: float, **kwargs: Any) -> Any:
        boxes.append(run_timeout_s)
        raise SandboxError("stop here")

    monkeypatch.setattr(work_module, "Sandbox", sandbox)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=_git_env())
    store = HarnessStore(tmp_path / "harness")
    store.init()
    runner = WorkRunner(
        repo=repo,
        config=Config(schema_version=3, project="p"),
        task_text="t",
        check="true",
        harness_dir=store.path,
        state=tmp_path / "state",
        client_factory=lambda: FakeClient([]),
        docker_client=object(),
        image="img",
    )
    with pytest.raises(EnvironmentFailure):
        runner.attempt(default_loop_config(), seed=7, deadline=time.monotonic() + 42)
    assert 41 < boxes[0] <= 42  # what was left once the workspace was ready


def test_a_sandbox_that_fails_is_an_environment_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=_git_env())
    store = HarnessStore(tmp_path / "harness")
    store.init()

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise SandboxError("cannot start sandbox from img: no such image")

    monkeypatch.setattr(work_module, "Sandbox", broken)
    runner = WorkRunner(
        repo=repo,
        config=Config(schema_version=3, project="p"),
        task_text="Do nothing.",
        check="true",
        harness_dir=store.path,
        state=tmp_path / "state",
        client_factory=lambda: FakeClient([]),
        docker_client=object(),
        image="img",
    )
    with pytest.raises(EnvironmentFailure, match="no such image"):
        runner.attempt(default_loop_config(), seed=7)


def test_no_time_left_is_a_timeout_without_a_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def sandbox(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no sandbox may start")

    monkeypatch.setattr(work_module, "Sandbox", sandbox)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=_git_env())
    store = HarnessStore(tmp_path / "harness")
    store.init()
    runner = WorkRunner(
        repo=repo,
        config=Config(schema_version=3, project="p"),
        task_text="t",
        check="true",
        harness_dir=store.path,
        state=tmp_path / "state",
        client_factory=lambda: FakeClient([]),
        docker_client=object(),
        image="img",
    )
    probe = runner.attempt(default_loop_config(), seed=7, deadline=time.monotonic() - 1)
    assert (probe.passed, probe.tags) == (False, ("timeout",))


def test_a_check_that_timed_out_did_not_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SlowBox(FakeBox):
        def exec(self, command: str, timeout_s: float) -> ExecResult:
            if command == "true":  # the final check: it caught the signal and exited 0
                return ExecResult(exit_code=0, output="", timed_out=True, duration_s=60)
            return super().exec(command, timeout_s)

    clock = Clock()
    monkeypatch.setattr(
        work_module, "Sandbox", lambda *a, run_timeout_s, **k: SlowBox(clock, run_timeout_s)
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=_git_env())
    store = HarnessStore(tmp_path / "harness")
    store.init()
    loop = default_loop_config().model_copy(
        update={"finish_gate": default_loop_config().finish_gate.model_copy(update={"run": None})}
    )
    runner = WorkRunner(
        repo=repo,
        config=Config(schema_version=3, project="p"),
        task_text="t",
        check="true",
        harness_dir=store.path,
        state=tmp_path / "state",
        client_factory=lambda: FakeClient(
            lambda m, t, p: ModelReply(
                tool_calls=(ToolCall(id="f", name="finish", arguments={"summary": "x"}),)
            )
        ),
        docker_client=object(),
        image="img",
    )
    assert not runner.attempt(loop, seed=7).passed


def test_a_failing_model_runner_is_an_environment_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    monkeypatch.setattr(
        work_module, "Sandbox", lambda *a, run_timeout_s, **k: FakeBox(clock, run_timeout_s)
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=_git_env())
    store = HarnessStore(tmp_path / "harness")
    store.init()

    def down(messages: Any, tools: Any, params: Any) -> ModelReply:
        raise ProviderError("connection refused")

    runner = WorkRunner(
        repo=repo,
        config=Config(schema_version=3, project="p"),
        task_text="t",
        check="true",  # would pass: the workspace satisfies it
        harness_dir=store.path,
        state=tmp_path / "state",
        client_factory=lambda: FakeClient(down),
        docker_client=object(),
        image="img",
    )
    with pytest.raises(EnvironmentFailure, match="model runner failed"):
        runner.attempt(default_loop_config(), seed=7)


def test_container_start_up_counts_against_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()

    class SlowStart(FakeBox):
        def __enter__(self) -> "FakeBox":
            clock.now += 20  # Docker takes twenty seconds to start the container
            self.deadline = clock.now + self.run_s  # what Sandbox.start does
            return self

    boxes: list[SlowStart] = []

    def sandbox(*args: Any, run_timeout_s: float, **kwargs: Any) -> SlowStart:
        box = SlowStart(clock, run_timeout_s)
        box.run_s = run_timeout_s
        boxes.append(box)
        return box

    monkeypatch.setattr(work_module, "Sandbox", sandbox)
    monkeypatch.setattr(work_module.time, "monotonic", lambda: clock.now)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=_git_env())
    store = HarnessStore(tmp_path / "harness")
    store.init()
    loop = default_loop_config().model_copy(
        update={"finish_gate": default_loop_config().finish_gate.model_copy(update={"run": None})}
    )
    runner = WorkRunner(
        repo=repo,
        config=Config(schema_version=3, project="p"),
        task_text="t",
        check="true",
        harness_dir=store.path,
        state=tmp_path / "state",
        client_factory=lambda: FakeClient(
            lambda m, t, p: ModelReply(
                tool_calls=(ToolCall(id="f", name="finish", arguments={"summary": "x"}),)
            )
        ),
        docker_client=object(),
        image="img",
    )
    runner.attempt(loop, seed=7, deadline=clock.now + 42)
    # 42 s were left when the attempt began; 20 went to start-up, so only 22 remain
    assert boxes[0].deadline == pytest.approx(1000.0 + 42)
    assert boxes[0].checks[-1] == pytest.approx(22)


def test_a_tool_command_cannot_run_into_the_checks_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    boxes: list[FakeBox] = []

    def sandbox(*args: Any, run_timeout_s: float, **kwargs: Any) -> FakeBox:
        boxes.append(FakeBox(clock, run_timeout_s))
        return boxes[-1]

    monkeypatch.setattr(work_module, "Sandbox", sandbox)
    monkeypatch.setattr(work_module.time, "monotonic", lambda: clock.now)
    calls = iter(
        [
            ToolCall(id="b", name="bash", arguments={"command": "sleep 999", "timeout": 600}),
            ToolCall(id="f", name="finish", arguments={"summary": "x"}),
        ]
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=_git_env())
    store = HarnessStore(tmp_path / "harness")
    store.init()
    loop = default_loop_config().model_copy(
        update={"finish_gate": default_loop_config().finish_gate.model_copy(update={"run": None})}
    )
    runner = WorkRunner(
        repo=repo,
        config=Config(
            schema_version=3, project="p", evaluation=EvaluationConfig(timeout_seconds=TIMEOUT)
        ),
        task_text="t",
        check="true",
        harness_dir=store.path,
        state=tmp_path / "state",
        client_factory=lambda: FakeClient(lambda m, t, p: ModelReply(tool_calls=(next(calls),))),
        docker_client=object(),
        image="img",
    )
    runner.attempt(loop, seed=7)
    tool, check = boxes[0].checks[0], boxes[0].checks[-1]
    assert tool == pytest.approx(TIMEOUT * STUDENT_SHARE)  # asked for 600 s, given its share
    assert check == pytest.approx(TIMEOUT)  # the clock did not move: the check has it all
