"""The ``ModelClient`` protocol and the types it exchanges (B5)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

ToolMode = Literal["native", "json"]
Role = Literal["system", "user", "assistant", "tool"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolCall(_Frozen):
    id: str
    name: str
    arguments: dict[str, Any]


class Message(_Frozen):
    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


class ToolSpec(_Frozen):
    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})


class GenParams(_Frozen):
    temperature: float = 0.2
    seed: int | None = None
    max_tokens: int | None = None
    tool_mode: ToolMode = "native"
    # loop.yaml's reasoning.effort (B6); None leaves the runner's default
    reasoning: Literal["off", "low", "high"] | None = None


class Usage(_Frozen):
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ModelInfo(_Frozen):
    provider: str
    model: str
    digest: str | None = None
    context_length: int | None = None
    tool_mode: ToolMode | None = None
    runner: str | None = None
    runner_version: str | None = None


class ModelReply(_Frozen):
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    raw: Any = None
    usage: Usage = Usage()
    latency_s: float = 0.0
    repaired: bool = False
    # Tool calls that could not be parsed even after repair; the loop returns each to the
    # model as a tool error and counts it toward ``malformed_tool_call``.
    malformed: tuple[str, ...] = ()


@runtime_checkable
class ModelClient(Protocol):
    def info(self) -> ModelInfo: ...

    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None,
        params: GenParams,
    ) -> ModelReply: ...


class ProviderError(Exception):
    """A model call failed and should not be retried."""


class TransientProviderError(ProviderError):
    """A model call failed in a way worth retrying: 429, 5xx, connection reset, timeout."""


class ContextLengthError(ProviderError):
    """The prompt does not fit the model's context window."""
