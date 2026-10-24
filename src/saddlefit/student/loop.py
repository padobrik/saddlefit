"""The student agent loop (B6).

Assemble the context, call the model, execute tool calls in the sandbox, append results,
detect loops, compact when the context passes 80% of its budget, and repeat until
``finish`` passes its gate, the step limit or the time limit.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from saddlefit.context.assembly import AssembledContext, estimate_tokens
from saddlefit.harness.loop_config import LoopConfig
from saddlefit.logs import redact
from saddlefit.providers.base import (
    ContextLengthError,
    GenParams,
    Message,
    ModelClient,
    ProviderError,
    ToolCall,
    ToolSpec,
)
from saddlefit.tools.builtin import TOOL_SPECS, ToolResult, truncate

COMPACTION_RATIO = 0.8
LOGGED_ARGUMENT_CHARS = 300
NUDGE = "Use a tool to make progress, or call finish when the task is done."

StopReason = Literal[
    "finished", "step_limit", "time_limit", "loop", "context_overflow", "provider_error"
]


class Tools(Protocol):
    def call(self, name: str, arguments: dict[str, object]) -> ToolResult: ...


class Recorder(Protocol):
    def event(self, type: str, data: dict[str, object] | None = None) -> None: ...


@dataclass
class StudentResult:
    """How the run ended, plus the signals the failure taxonomy needs."""

    stop_reason: StopReason
    steps: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    max_prompt_tokens: int = 0
    malformed_tool_calls: int = 0
    max_failing_tool_streak: int = 0
    ran_visible_check: bool = False
    finish_gate_passed: bool | None = None
    stopped_by_loop_detection: bool = False
    context_length_error: bool = False
    compactions: int = 0
    transcript: list[Message] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        return self.stop_reason == "finished"


def call_signature(call: ToolCall) -> str:
    """Tool name plus a hash of normalized arguments (B6 loop detection)."""
    args = json.dumps(call.arguments, sort_keys=True, separators=(",", ":"))
    return f"{call.name}:{hashlib.sha256(args.encode()).hexdigest()[:16]}"


def logged_arguments(arguments: dict[str, object]) -> dict[str, str | int | float | bool | None]:
    """The arguments for the run record: scalars kept, the rest as redacted, shortened text."""
    logged: dict[str, str | int | float | bool | None] = {}
    for key, value in arguments.items():
        if (
            value is None
            or isinstance(value, bool | int)
            or (isinstance(value, float) and math.isfinite(value))
        ):
            logged[key] = value
        else:
            text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
            # redacted before the cut, so a key cut in half is never half-kept
            logged[key] = truncate(redact(text), LOGGED_ARGUMENT_CHARS, hint="")
    return logged


@dataclass
class _RunState:
    context: AssembledContext
    messages: list[Message]
    result: StudentResult
    gate_retries: int
    check: str | None
    seen: dict[str, tuple[int, str]] = field(default_factory=dict)
    warned: set[str] = field(default_factory=set)
    failing_streak: int = 0


@dataclass
class StudentLoop:
    """Runs one task: the model works through tools until it finishes or a limit stops it."""

    client: ModelClient
    tools: Tools
    loop: LoopConfig
    params: GenParams
    recorder: Recorder
    visible_check: str | None
    time_limit_s: float
    workspace_digest: Callable[[], str]
    tool_specs: tuple[ToolSpec, ...] = TOOL_SPECS
    clock: Callable[[], float] = time.monotonic

    def run(self, context: AssembledContext) -> StudentResult:
        started = self.clock()
        state = _RunState(
            context=context,
            messages=[
                Message(role="system", content=context.system),
                Message(role="user", content=context.task),
            ],
            result=StudentResult(stop_reason="step_limit"),
            gate_retries=self.loop.finish_gate.max_retries,
            check=self.visible_check or self.loop.finish_gate.run,
        )
        reason: StopReason = "step_limit"
        for step in range(1, self.loop.max_steps + 1):
            if self.clock() - started >= self.time_limit_s:
                reason = "time_limit"
                break
            state.result.steps = step  # counted only once the step actually runs
            stopped = self._step(state, step)
            if stopped is not None:
                reason = stopped
                break
        state.result.stop_reason = reason
        state.result.transcript = state.messages
        return state.result

    def _step(self, state: _RunState, step: int) -> StopReason | None:
        result = state.result
        state.messages = self._compact(state.messages, result)
        try:
            reply = self.client.chat(state.messages, self.tool_specs, self.params)
        except ContextLengthError as exc:
            result.context_length_error = True
            self.recorder.event("error", {"kind": "context_length", "message": str(exc)})
            return "context_overflow"
        except ProviderError as exc:
            # the runner failed (timeouts after retries, a dead endpoint): an env_error run
            self.recorder.event("error", {"kind": "provider", "message": str(exc)[:2000]})
            return "provider_error"
        result.prompt_tokens += reply.usage.prompt_tokens
        result.completion_tokens += reply.usage.completion_tokens
        result.max_prompt_tokens = max(result.max_prompt_tokens, reply.usage.prompt_tokens)
        self.recorder.event(
            "model_call",
            {
                "step": step,
                "prompt_tokens": reply.usage.prompt_tokens,
                "completion_tokens": reply.usage.completion_tokens,
                "tool_calls": [c.name for c in reply.tool_calls],
                "malformed": len(reply.malformed),
                "repaired": reply.repaired,
            },
        )
        state.messages.append(
            Message(role="assistant", content=reply.text, tool_calls=reply.tool_calls)
        )
        result.malformed_tool_calls += len(reply.malformed)
        if not reply.tool_calls and not reply.malformed:
            state.messages.append(Message(role="user", content=NUDGE))
        for call in reply.tool_calls:
            stopped = self._handle(state, call)
            if stopped is not None:
                return stopped
        # after the tool results: native tool APIs expect each tool call's answer next
        for raw in reply.malformed:
            state.messages.append(
                Message(
                    role="user",
                    content=f"Your tool call could not be parsed and was ignored:\n{raw[:500]}",
                )
            )
        return None

    def _handle(self, state: _RunState, call: ToolCall) -> StopReason | None:
        looping = self._loop_check(state, call)
        if looping == "stop":
            return "loop"
        if looping == "warn":
            state.messages.append(
                Message(
                    role="tool",
                    tool_call_id=call.id,
                    content="You repeated the same call without changing anything. "
                    "Try a different approach.",
                )
            )
            return None
        if call.name == "finish":
            return self._finish_gate(state, call)
        outcome = self.tools.call(call.name, call.arguments)
        if (
            state.check
            and call.name == "bash"
            and state.check in str(call.arguments.get("command"))
        ):
            state.result.ran_visible_check = True
        state.failing_streak = 0 if outcome.ok else state.failing_streak + 1
        state.result.max_failing_tool_streak = max(
            state.result.max_failing_tool_streak, state.failing_streak
        )
        self.recorder.event(
            "tool_call",
            {
                "name": call.name,
                "ok": outcome.ok,
                "output_chars": len(outcome.output),
                "arguments": logged_arguments(call.arguments),
            },
        )
        state.messages.append(Message(role="tool", tool_call_id=call.id, content=outcome.output))
        return None

    def _loop_check(self, state: _RunState, call: ToolCall) -> Literal["ok", "warn", "stop"]:
        """N identical calls with no workspace change: warn once, then stop (B6)."""
        signature = call_signature(call)
        digest = self.workspace_digest()
        count, last_digest = state.seen.get(signature, (0, ""))
        count = count + 1 if last_digest == digest else 1
        state.seen[signature] = (count, digest)
        if count < self.loop.loop_detection.identical_calls:
            return "ok"
        if signature in state.warned:
            state.result.stopped_by_loop_detection = True
            self.recorder.event("loop_warning", {"signature": signature, "stop": True})
            return "stop"
        state.warned.add(signature)
        self.recorder.event("loop_warning", {"signature": signature, "stop": False})
        return "warn"

    def _finish_gate(self, state: _RunState, call: ToolCall) -> StopReason | None:
        """Run the visible check on finish; on failure return its output to the model once."""
        if state.check is None:
            return "finished"
        gate = self.tools.call("bash", {"command": state.check})
        state.result.ran_visible_check = True
        state.result.finish_gate_passed = gate.ok
        self.recorder.event("finish_gate", {"passed": gate.ok, "output": gate.output})
        if gate.ok or state.gate_retries <= 0:
            return "finished"
        state.gate_retries -= 1
        reminder = (
            f"\n\nThe task again:\n{state.context.task}"
            if self.loop.finish_gate.reread_task
            else ""
        )
        state.messages.append(
            Message(
                role="tool",
                tool_call_id=call.id,
                content=f"The check `{state.check}` failed:\n{gate.output}{reminder}",
            )
        )
        return None

    def _compact(self, messages: list[Message], result: StudentResult) -> list[Message]:
        """Drop the oldest tool outputs first, leaving a stub; keep the task and the latest."""
        budget = self.loop.context.budget_tokens * COMPACTION_RATIO

        def size(msgs: list[Message]) -> int:
            # tool-call arguments (a whole new file for write_file) are part of the prompt too
            return sum(
                estimate_tokens(m.content)
                + sum(estimate_tokens(json.dumps(c.arguments)) for c in m.tool_calls)
                for m in msgs
            )

        if size(messages) <= budget:
            return messages
        compacted = list(messages)
        tool_indexes = [i for i, m in enumerate(compacted) if m.role == "tool"]
        dropped = 0
        for i in tool_indexes[:-1]:  # the latest tool output (often the latest error) stays
            if size(compacted) <= budget:
                break
            original = compacted[i]
            if original.content.startswith("[output dropped"):
                continue
            stub = (
                f"[output dropped to save context; {len(original.content)} chars; re-run if needed]"
            )
            compacted[i] = Message(role="tool", tool_call_id=original.tool_call_id, content=stub)
            dropped += 1
        if dropped:
            result.compactions += 1
            self.recorder.event("compaction", {"dropped_outputs": dropped})
        return compacted
