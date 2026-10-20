"""A deterministic provider that replays scripted replies (B5), for tests."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

from saddlefit.providers.base import (
    GenParams,
    Message,
    ModelInfo,
    ModelReply,
    ProviderError,
    ToolCall,
    ToolSpec,
    Usage,
)
from saddlefit.providers.parsing import MalformedToolCall, parse_json_tool_call

Script = Callable[[Sequence[Message], Sequence[ToolSpec] | None, GenParams], ModelReply | str]


class FakeClient:
    """Replays replies in order, or asks a script function for each reply.

    A string reply is treated as raw model text: in ``json`` tool mode it is parsed like a
    real model's answer, so tests exercise the same tolerant parser.
    """

    def __init__(
        self,
        replies: Sequence[ModelReply | str] | Script,
        *,
        model: str = "fake-model",
        context_length: int = 32768,
    ) -> None:
        self._script = replies if callable(replies) else None
        self._queue = [] if callable(replies) else list(replies)
        self._model = model
        self._context_length = context_length
        self.calls: list[tuple[tuple[Message, ...], GenParams]] = []

    @classmethod
    def from_fixture(
        cls, path: Path, *, model: str = "fake-model", context_length: int = 32768
    ) -> FakeClient:
        """Load replies from a JSON Lines file of ``ModelReply`` objects or strings."""
        replies: list[ModelReply | str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                replies.append(item if isinstance(item, str) else ModelReply.model_validate(item))
        return cls(replies, model=model, context_length=context_length)

    def info(self) -> ModelInfo:
        return ModelInfo(
            provider="fake",
            model=self._model,
            digest="sha256:fake",
            context_length=self._context_length,
            runner="fake",
            runner_version="0",
        )

    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None,
        params: GenParams,
    ) -> ModelReply:
        self.calls.append((tuple(messages), params))
        if self._script is not None:
            reply = self._script(messages, tools, params)
        elif self._queue:
            reply = self._queue.pop(0)
        else:
            raise ProviderError("fake provider has no scripted replies left")
        if isinstance(reply, ModelReply):
            return reply
        return _from_text(reply, tools or (), params, len(self.calls))


def _from_text(text: str, tools: Sequence[ToolSpec], params: GenParams, n: int) -> ModelReply:
    usage = Usage(prompt_tokens=0, completion_tokens=max(1, len(text) // 3))
    if params.tool_mode != "json" or not tools:
        return ModelReply(text=text, raw=text, usage=usage)
    try:
        name, args, repaired = parse_json_tool_call(text, tools)
    except MalformedToolCall:
        return ModelReply(text=text, raw=text, usage=usage, malformed=(text,))
    call = ToolCall(id=f"call_{n}", name=name, arguments=args)
    return ModelReply(raw=text, usage=usage, tool_calls=(call,), repaired=repaired)
