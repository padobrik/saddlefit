"""The ``openai-compatible`` student provider (B5).

Works with any OpenAI-compatible endpoint: Ollama (``/v1``), LM Studio, llama.cpp server,
vLLM. With Ollama it also reads the model digest and runner version from the native API.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import openai

from saddlefit.providers.base import (
    ContextLengthError,
    GenParams,
    Message,
    ModelInfo,
    ModelReply,
    ProviderError,
    ToolCall,
    ToolMode,
    ToolSpec,
    TransientProviderError,
    Usage,
)
from saddlefit.providers.parsing import (
    MalformedToolCall,
    extract_json_object,
    json_mode_schema,
    parse_json_tool_call,
)
from saddlefit.providers.retry import LOCAL_TIMEOUT_S, call_with_retries

log = logging.getLogger(__name__)

FetchJson = Callable[[str, dict[str, Any] | None], Any]

JSON_MODE_INSTRUCTIONS = (
    "Answer with exactly one JSON object and nothing else: "
    '{"tool": "<tool name>", "args": {...}}. Available tools:\n'
)
_CONTEXT_MARKERS = ("context length", "context window", "maximum context", "too many tokens")


def _fetch_json(url: str, payload: dict[str, Any] | None) -> Any:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(  # noqa: S310 - the URL comes from the user's config
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return json.load(response)


def _native_root(base_url: str) -> str:
    parts = urlsplit(base_url)
    path = parts.path.rstrip("/")
    path = path.removesuffix("/v1")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def to_openai_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Convert to the Chat Completions message format."""
    out: list[dict[str, Any]] = []
    for m in messages:
        item: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            item["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                }
                for c in m.tool_calls
            ]
        if m.tool_call_id is not None:
            item["tool_call_id"] = m.tool_call_id
        out.append(item)
    return out


def _tool_payload(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
        }
        for t in tools
    ]


def _json_mode_prompt(tools: Sequence[ToolSpec]) -> str:
    lines = [f"- {t.name}: {t.description} args schema: {json.dumps(t.parameters)}" for t in tools]
    return JSON_MODE_INSTRUCTIONS + "\n".join(lines)


def map_error(exc: Exception) -> ProviderError:
    """Classify an SDK error: transient errors are retried, everything else is not."""
    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return TransientProviderError(f"{type(exc).__name__}: {exc}")
    status = getattr(exc, "status_code", None)
    if status == 429 or (isinstance(status, int) and status >= 500):
        return TransientProviderError(f"HTTP {status}: {exc}")
    if any(marker in str(exc).lower() for marker in _CONTEXT_MARKERS):
        return ContextLengthError(str(exc))
    return ProviderError(f"{type(exc).__name__}: {exc}")


class OpenAICompatibleClient:
    """A student model behind an OpenAI-compatible Chat Completions endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        context_length: int | None = None,
        tool_mode: ToolMode | None = None,
        timeout_s: float = LOCAL_TIMEOUT_S,
        constrained_json: bool = True,
        sdk: Any = None,
        fetch_json: FetchJson = _fetch_json,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.context_length = context_length
        self.tool_mode = tool_mode
        self.constrained_json = constrained_json
        self._fetch_json = fetch_json
        self._sleep = sleep
        # Local runners ignore the key; a real key only ever comes from the environment.
        api_key = os.environ.get("OPENAI_API_KEY") or "not-needed"
        # Typed loosely: tests pass a stub with the same ``chat.completions.create`` shape.
        self._sdk: Any = sdk or openai.OpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout_s, max_retries=0
        )
        self._info: ModelInfo | None = None

    def info(self) -> ModelInfo:
        if self._info is None:
            self._info = self._probe_info()
        return self._info

    def _probe_info(self) -> ModelInfo:
        root = _native_root(self.base_url)
        runner, version, digest = "openai-compatible", None, None
        try:
            version = self._fetch_json(f"{root}/api/version", None).get("version")
            runner = "ollama"
            for entry in self._fetch_json(f"{root}/api/tags", None).get("models", []):
                if entry.get("name") == self.model or entry.get("model") == self.model:
                    digest = entry.get("digest")
                    digest = digest if digest is None or ":" in digest else f"sha256:{digest}"
                    break
        except (OSError, ValueError, AttributeError):
            log.debug("runner at %s does not expose the Ollama API", root)
        return ModelInfo(
            provider="openai-compatible",
            model=self.model,
            digest=digest,
            context_length=self.context_length,
            tool_mode=self.tool_mode,
            runner=runner,
            runner_version=version,
        )

    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None,
        params: GenParams,
    ) -> ModelReply:
        tools = tools or ()
        json_mode = params.tool_mode == "json" and bool(tools)
        payload: list[dict[str, Any]] = to_openai_messages(messages)
        kwargs: dict[str, Any] = {"model": self.model, "temperature": params.temperature}
        if params.seed is not None:
            kwargs["seed"] = params.seed
        if params.max_tokens is not None:
            kwargs["max_tokens"] = params.max_tokens
        if json_mode:
            payload = [{"role": "system", "content": _json_mode_prompt(tools)}, *payload]
            if self.constrained_json:
                kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "tool_call", "schema": json_mode_schema(tools)},
                }
        elif tools:
            kwargs["tools"] = _tool_payload(tools)

        def call() -> Any:
            try:
                return self._sdk.chat.completions.create(messages=payload, **kwargs)
            except openai.OpenAIError as exc:
                raise map_error(exc) from exc

        started = time.monotonic()
        response = call_with_retries(call, sleep=self._sleep)
        latency = time.monotonic() - started
        return self._to_reply(response, tools, json_mode, latency)

    def _to_reply(
        self, response: Any, tools: Sequence[ToolSpec], json_mode: bool, latency: float
    ) -> ModelReply:
        message = response.choices[0].message
        text = message.content or ""
        usage = Usage(
            prompt_tokens=getattr(response.usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(response.usage, "completion_tokens", 0) or 0,
        )
        raw = response.model_dump() if hasattr(response, "model_dump") else response
        calls: list[ToolCall] = []
        malformed: list[str] = []
        repaired = False
        if json_mode:
            try:
                name, args, repaired = parse_json_tool_call(text, tools)
                calls.append(ToolCall(id="call_0", name=name, arguments=args))
                text = ""
            except MalformedToolCall:
                malformed.append(text)
        for i, call in enumerate(message.tool_calls or []):
            arguments = call.function.arguments or "{}"
            try:
                args, was_repaired = extract_json_object(arguments)
            except MalformedToolCall:
                malformed.append(arguments)
                continue
            repaired = repaired or was_repaired
            calls.append(
                ToolCall(id=call.id or f"call_{i}", name=call.function.name, arguments=args)
            )
        return ModelReply(
            text=text,
            tool_calls=tuple(calls),
            raw=raw,
            usage=usage,
            latency_s=latency,
            repaired=repaired,
            malformed=tuple(malformed),
        )
