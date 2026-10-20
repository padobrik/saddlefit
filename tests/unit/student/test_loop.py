from dataclasses import dataclass, field
from typing import Any

import pytest

from saddlefit.context.assembly import AssembledContext
from saddlefit.harness.loop_config import (
    ContextConfig,
    FinishGateConfig,
    LoopConfig,
    LoopDetectionConfig,
    default_loop_config,
)
from saddlefit.providers.base import (
    ContextLengthError,
    GenParams,
    ModelReply,
    ToolCall,
    Usage,
)
from saddlefit.providers.fake import FakeClient
from saddlefit.student.loop import NUDGE, StudentLoop, call_signature
from saddlefit.tools.builtin import ToolResult

CONTEXT = AssembledContext(system="be careful", task="fix parse_iso", harness_tokens=3)


def call(name: str, i: int = 0, **args: Any) -> ModelReply:
    return ModelReply(
        tool_calls=(ToolCall(id=f"c{i}", name=name, arguments=args),),
        usage=Usage(prompt_tokens=100 + i, completion_tokens=10),
    )


@dataclass
class FakeTools:
    results: dict[str, list[ToolResult]] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        queue = self.results.get(name)
        return queue.pop(0) if queue else ToolResult(ok=True, output=f"{name} ok")


@dataclass
class Recorder:
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def event(self, type: str, data: dict[str, Any] | None = None) -> None:
        self.events.append((type, data or {}))

    def types(self) -> list[str]:
        return [t for t, _ in self.events]


class Clock:
    def __init__(self, step: float = 0.0) -> None:
        self.now, self.step = 0.0, step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def make(
    replies: list[Any],
    *,
    loop: LoopConfig | None = None,
    tools: FakeTools | None = None,
    check: str | None = "pytest -q tests/test_dates.py",
    time_limit: float = 900,
    digest: Any = None,
    clock: Clock | None = None,
) -> tuple[StudentLoop, FakeTools, Recorder]:
    tools = tools or FakeTools()
    recorder = Recorder()
    student = StudentLoop(
        client=FakeClient(replies),
        tools=tools,
        loop=loop or default_loop_config(),
        params=GenParams(),
        recorder=recorder,
        visible_check=check,
        time_limit_s=time_limit,
        workspace_digest=digest or (lambda: "same"),
        clock=clock or Clock(),
    )
    return student, tools, recorder


def test_happy_path_runs_tools_then_the_finish_gate() -> None:
    student, tools, recorder = make(
        [
            call("read_file", 1, path="src/dates.py"),
            call("edit_file", 2, path="src/dates.py", old="a", new="b"),
            call("finish", 3, summary="done"),
        ]
    )
    result = student.run(CONTEXT)
    assert result.finished
    assert result.finish_gate_passed is True
    assert result.ran_visible_check
    assert result.steps == 3
    assert (result.prompt_tokens, result.max_prompt_tokens) == (306, 103)
    assert tools.calls[-1] == ("bash", {"command": "pytest -q tests/test_dates.py"})
    assert recorder.types() == [
        "model_call",
        "tool_call",
        "model_call",
        "tool_call",
        "model_call",
        "finish_gate",
    ]
    assert result.transcript[0].content == "be careful"


def test_failed_gate_returns_output_once_with_the_task() -> None:
    tools = FakeTools(
        {
            "bash": [
                ToolResult(ok=False, output="1 failed"),
                ToolResult(ok=False, output="still failing"),
            ]
        }
    )
    student, _, _ = make(
        [call("finish", 1, summary="x"), call("finish", 2, summary="y")], tools=tools
    )
    result = student.run(CONTEXT)
    assert result.finished
    assert result.finish_gate_passed is False
    feedback = [m.content for m in result.transcript if m.role == "tool"]
    assert feedback == [
        "The check `pytest -q tests/test_dates.py` failed:\n1 failed\n\n"
        "The task again:\nfix parse_iso"
    ]


def test_no_check_means_finish_ends_the_run() -> None:
    loop = default_loop_config().model_copy(update={"finish_gate": FinishGateConfig(run=None)})
    student, tools, _ = make([call("finish", 1, summary="x")], loop=loop, check=None)
    result = student.run(CONTEXT)
    assert result.finished
    assert result.finish_gate_passed is None
    assert tools.calls == []


def test_step_and_time_limits() -> None:
    loop = default_loop_config().model_copy(update={"max_steps": 2})
    student, _, _ = make([call("list_dir", 1), call("list_dir", 2, path="x")], loop=loop)
    assert student.run(CONTEXT).stop_reason == "step_limit"
    student, _, _ = make([call("list_dir", 1)], time_limit=1, clock=Clock(step=0.7))
    result = student.run(CONTEXT)
    assert result.stop_reason == "time_limit"


def test_loop_detection_warns_then_stops() -> None:
    loop = default_loop_config().model_copy(
        update={"loop_detection": LoopDetectionConfig(identical_calls=2)}
    )
    same = [call("read_file", i, path="a.py") for i in range(5)]
    student, tools, recorder = make(same, loop=loop)
    result = student.run(CONTEXT)
    assert result.stop_reason == "loop"
    assert result.stopped_by_loop_detection
    assert [d["stop"] for t, d in recorder.events if t == "loop_warning"] == [False, True]
    assert len(tools.calls) == 1


def test_a_workspace_change_resets_loop_detection() -> None:
    loop = default_loop_config().model_copy(
        update={"loop_detection": LoopDetectionConfig(identical_calls=2), "max_steps": 4}
    )
    digests = iter(["a", "b", "c", "d", "e"])
    student, _, _ = make(
        [call("bash", i, command="pytest") for i in range(4)],
        loop=loop,
        digest=lambda: next(digests),
    )
    assert student.run(CONTEXT).stop_reason == "step_limit"


def test_malformed_calls_nudges_and_tool_failure_streaks() -> None:
    tools = FakeTools({"bash": [ToolResult(ok=False, output="e")] * 3})
    replies = [
        ModelReply(malformed=("{bad",)),
        ModelReply(text="I think..."),
        call("bash", 1, command="a"),
        call("bash", 2, command="b"),
        call("bash", 3, command="c"),
        call("finish", 4, summary="x"),
    ]
    student, _, _ = make(replies, tools=tools)
    result = student.run(CONTEXT)
    assert result.malformed_tool_calls == 1
    assert result.max_failing_tool_streak == 3
    contents = [m.content for m in result.transcript]
    assert any("could not be parsed" in c for c in contents)
    assert NUDGE in contents


def test_running_the_visible_check_by_hand_counts() -> None:
    student, _, _ = make(
        [call("bash", 1, command="pytest -q tests/test_dates.py -x")],
        loop=default_loop_config().model_copy(update={"max_steps": 1}),
    )
    assert student.run(CONTEXT).ran_visible_check


def test_context_length_error_stops_the_run() -> None:
    def script(messages: Any, tools: Any, params: Any) -> ModelReply:
        raise ContextLengthError("too long")

    student, _, recorder = make([])
    student.client = FakeClient(script)
    result = student.run(CONTEXT)
    assert result.stop_reason == "context_overflow"
    assert result.context_length_error
    assert recorder.types() == ["error"]


def test_compaction_drops_old_tool_outputs_but_keeps_the_task_and_latest() -> None:
    loop = default_loop_config().model_copy(
        update={
            "context": ContextConfig(budget_tokens=200, harness_budget_tokens=100),
            "max_steps": 4,
        }
    )
    # each output (600 chars, about 200 tokens) alone exceeds 80% of the budget, so only the
    # rule "the latest output stays" keeps the last one
    tools = FakeTools({"read_file": [ToolResult(ok=True, output="x" * 600)] * 3})
    replies = [call("read_file", i, path=f"f{i}.py") for i in range(3)] + [call("finish", 9)]
    student, _, recorder = make(replies, loop=loop, tools=tools, check=None)
    result = student.run(CONTEXT)
    tool_msgs = [m.content for m in result.transcript if m.role == "tool"]
    assert tool_msgs[0].startswith("[output dropped")
    assert tool_msgs[-1] == "x" * 600
    assert all(m.startswith("[output dropped") for m in tool_msgs[:-1])
    assert any(m.content == "fix parse_iso" for m in result.transcript)
    assert result.compactions >= 1
    assert "compaction" in recorder.types()


def test_signature_ignores_argument_order() -> None:
    a = ToolCall(id="1", name="search", arguments={"pattern": "x", "path": "."})
    b = ToolCall(id="2", name="search", arguments={"path": ".", "pattern": "x"})
    assert call_signature(a) == call_signature(b)
    assert call_signature(a) != call_signature(
        ToolCall(id="3", name="search", arguments={"pattern": "y"})
    )


@pytest.mark.parametrize("identical", [2, 3])
def test_identical_call_threshold_comes_from_loop_yaml(identical: int) -> None:
    loop = default_loop_config().model_copy(
        update={"loop_detection": LoopDetectionConfig(identical_calls=identical)}
    )
    student, tools, _ = make([call("list_dir", i) for i in range(10)], loop=loop)
    student.run(CONTEXT)
    assert len(tools.calls) == identical - 1
