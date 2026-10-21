"""The ``anthropic`` provider for the tutor and the generator: the Messages API (B5).

Structured proposals come back through tool use. Current Claude models refuse a forced
``tool_choice``, so tools are offered with ``tool_choice: auto`` and ``strict: true`` (the
input then matches the schema exactly) and the prompt asks for the call.

The system prompt is cached with the 1-hour TTL: a round's tutor calls share it, and the
batch reruns between them take longer than the 5-minute TTL. Thinking blocks are not kept
in ``Message``, so a conversation that replays assistant turns runs without them.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any, Final

import anthropic

from saddlefit.providers.base import (
    ContextLengthError,
    GenParams,
    Message,
    ModelInfo,
    ModelReply,
    ProviderError,
    ToolCall,
    ToolSpec,
    TransientProviderError,
    Usage,
)
from saddlefit.providers.retry import API_TIMEOUT_S, call_with_retries

DEFAULT_MAX_TOKENS: Final = 16_000
CACHE_CONTROL: Final = {"type": "ephemeral", "ttl": "1h"}
# On a policy refusal the API reruns the call on a model chosen by refusal category.
FALLBACK_BETA: Final = "server-side-fallback-2026-07-01"
_EFFORT: Final = {"off": "low", "low": "low", "high": "high"}  # thinking cannot be disabled
_TRANSIENT_STATUS: Final = frozenset({408, 409, 429})
_CONTEXT_MARKERS: Final = ("prompt is too long", "context window", "too many tokens")
# The SDK reports missing credentials at request time with a TypeError, not an API error.
_NO_CREDENTIALS: Final = "Could not resolve authentication method"
NO_KEY_HINT: Final = "no Anthropic credentials found; set ANTHROPIC_API_KEY"


def to_request(messages: Sequence[Message]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split ``messages`` into Messages API ``system`` blocks and ``messages``."""
    system: list[dict[str, Any]] = [
        {"type": "text", "text": m.content} for m in messages if m.role == "system"
    ]
    if system:
        system[-1]["cache_control"] = dict(CACHE_CONTROL)
    turns: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            turns.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            blocks: list[dict[str, Any]] = (
                [{"type": "text", "text": m.content}] if m.content else []
            )
            blocks += [
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                for c in m.tool_calls
            ]
            turns.append({"role": "assistant", "content": blocks})
        elif m.role == "tool":
            result = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
            last = turns[-1] if turns else None
            # every result for one assistant turn goes back in a single user message
            if last and last["role"] == "user" and isinstance(last["content"], list):
                last["content"].append(result)
            else:
                turns.append({"role": "user", "content": [result]})
    return system, turns


def to_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.parameters,
            "strict": True,
        }
        for t in tools
    ]


def to_usage(usage: Any) -> Usage:
    """Usage with cache writes split by TTL; ``input_tokens`` excludes cached input."""
    creation = getattr(usage, "cache_creation", None)
    return Usage(
        prompt_tokens=usage.input_tokens,
        completion_tokens=usage.output_tokens,
        cache_write_5m_tokens=getattr(creation, "ephemeral_5m_input_tokens", 0) or 0,
        cache_write_1h_tokens=getattr(creation, "ephemeral_1h_input_tokens", 0) or 0,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
    )


def to_reply(response: Any, latency_s: float) -> ModelReply:
    if response.stop_reason == "refusal":
        category = getattr(response.stop_details, "category", None)
        raise ProviderError(f"the model declined the request (category {category})")
    if response.stop_reason == "max_tokens":
        raise ProviderError("the reply was cut off at max_tokens")
    text = "".join(b.text for b in response.content if b.type == "text")
    calls = tuple(
        ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
        for b in response.content
        if b.type == "tool_use"
    )
    return ModelReply(
        text=text,
        tool_calls=calls,
        raw=response,
        usage=to_usage(response.usage),
        latency_s=latency_s,
    )


def classify_error(exc: anthropic.AnthropicError) -> ProviderError:
    """Map an SDK error to a retryable or a final provider error."""
    if isinstance(exc, anthropic.APIConnectionError):  # includes timeouts
        return TransientProviderError(f"connection to the Anthropic API failed: {exc}")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code in _TRANSIENT_STATUS or exc.status_code >= 500:
            return TransientProviderError(f"Anthropic API error {exc.status_code}")
        message = str(exc.message).lower()
        if exc.status_code in {400, 413} and any(m in message for m in _CONTEXT_MARKERS):
            return ContextLengthError(f"prompt does not fit the context window: {exc.message}")
        return ProviderError(f"Anthropic API error {exc.status_code}: {exc.message}")
    return ProviderError(f"Anthropic client error: {exc}")


class AnthropicClient:
    """A ``ModelClient`` for Claude; the key comes only from the environment."""

    def __init__(self, model: str, client: Any = None, timeout_s: float = API_TIMEOUT_S) -> None:
        self._model = model
        if client is None:
            try:
                # retries are ours (B5), so the SDK makes exactly one attempt per call
                client = anthropic.Anthropic(max_retries=0, timeout=timeout_s)
            except anthropic.AnthropicError as exc:
                raise ProviderError(f"{NO_KEY_HINT}: {exc}") from exc
        self._client = client

    def info(self) -> ModelInfo:
        return ModelInfo(provider="anthropic", model=self._model, tool_mode="native")

    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None,
        params: GenParams,
    ) -> ModelReply:
        system, turns = to_request(messages)
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": params.max_tokens or DEFAULT_MAX_TOKENS,
            "messages": turns,
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = to_tools(tools)
            request["tool_choice"] = {"type": "auto"}
        if params.reasoning is not None:
            request["output_config"] = {"effort": _EFFORT[params.reasoning]}

        def call() -> ModelReply:
            start = time.monotonic()
            try:
                response = self._client.beta.messages.create(**request)
            except anthropic.AnthropicError as exc:
                raise classify_error(exc) from exc
            except TypeError as exc:
                if _NO_CREDENTIALS not in str(exc):
                    raise
                raise ProviderError(NO_KEY_HINT) from exc
            return to_reply(response, time.monotonic() - start)

        return call_with_retries(call)
