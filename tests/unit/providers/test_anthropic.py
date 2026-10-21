"""The Anthropic provider against a stub client: request shape, replies, usage and errors."""

from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types.beta import BetaMessage

from saddlefit.providers import retry
from saddlefit.providers.anthropic import AnthropicClient, classify_error, to_request
from saddlefit.providers.base import (
    ContextLengthError,
    GenParams,
    Message,
    ModelClient,
    ProviderError,
    ToolCall,
    ToolSpec,
    TransientProviderError,
)

PROPOSE = ToolSpec(
    name="propose",
    description="Submit one proposal.",
    parameters={
        "type": "object",
        "properties": {"technique": {"type": "string"}},
        "required": ["technique"],
        "additionalProperties": False,
    },
)


def message(content: list[dict[str, Any]], stop_reason: str = "end_turn", **usage: Any) -> Any:
    return BetaMessage.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                **usage,
            },
        }
    )


class Stub:
    """Stands in for ``anthropic.Anthropic``; replays replies or raises errors in order."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[dict[str, Any]] = []
        self.beta = self
        self.messages = self

    def create(self, **request: Any) -> Any:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def status_error(cls: type[anthropic.APIStatusError], status: int, text: str = "x") -> Any:
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://api.invalid"))
    return cls(text, response=response, body=None)


def test_it_is_a_model_client() -> None:
    assert isinstance(AnthropicClient("claude-opus-5-5", client=Stub()), ModelClient)


def test_request_shape() -> None:
    stub = Stub(message([{"type": "text", "text": "ok"}]))
    client = AnthropicClient("claude-opus-5-5", client=stub)
    client.chat(
        [Message(role="system", content="You diagnose."), Message(role="user", content="Runs…")],
        [PROPOSE],
        GenParams(temperature=0.7, seed=3),
    )
    request = stub.requests[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["system"] == [
        {
            "type": "text",
            "text": "You diagnose.",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
    ]
    assert request["messages"] == [{"role": "user", "content": "Runs…"}]
    assert request["tools"][0]["strict"] is True
    assert request["tools"][0]["input_schema"] == PROPOSE.parameters
    # forced tool choice is a 400 on current models; sampling settings are rejected too
    assert request["tool_choice"] == {"type": "auto"}
    assert {"temperature", "seed", "top_p", "output_config"}.isdisjoint(request)
    assert request["fallbacks"] == "default"
    assert request["betas"] == ["server-side-fallback-2026-07-01"]
    assert request["max_tokens"] == 16_000


def test_reasoning_maps_to_effort() -> None:
    stub = Stub(message([{"type": "text", "text": "ok"}]))
    AnthropicClient("m", client=stub).chat(
        [Message(role="user", content="hi")], None, GenParams(reasoning="off", max_tokens=500)
    )
    assert stub.requests[0]["output_config"] == {"effort": "low"}
    assert stub.requests[0]["max_tokens"] == 500
    assert "tools" not in stub.requests[0]
    assert "system" not in stub.requests[0]


def test_tool_results_share_one_user_message() -> None:
    calls = (
        ToolCall(id="a", name="read", arguments={"p": "x"}),
        ToolCall(id="b", name="read", arguments={"p": "y"}),
    )
    _, turns = to_request(
        [
            Message(role="user", content="go"),
            Message(role="assistant", content="", tool_calls=calls),
            Message(role="tool", content="X", tool_call_id="a"),
            Message(role="tool", content="Y", tool_call_id="b"),
        ]
    )
    assert turns[1] == {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "a", "name": "read", "input": {"p": "x"}},
            {"type": "tool_use", "id": "b", "name": "read", "input": {"p": "y"}},
        ],
    }
    assert turns[2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "X"},
            {"type": "tool_result", "tool_use_id": "b", "content": "Y"},
        ],
    }


def test_a_tool_call_reply_with_cache_usage() -> None:
    reply_message = message(
        [
            {"type": "text", "text": "Here is my proposal."},
            {"type": "tool_use", "id": "t1", "name": "propose", "input": {"technique": "gate"}},
        ],
        stop_reason="tool_use",
        input_tokens=40,
        cache_read_input_tokens=3000,
        cache_creation_input_tokens=500,
        cache_creation={"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 500},
    )
    reply = AnthropicClient("m", client=Stub(reply_message)).chat(
        [Message(role="user", content="hi")], [PROPOSE], GenParams()
    )
    assert reply.text == "Here is my proposal."
    assert reply.tool_calls == (ToolCall(id="t1", name="propose", arguments={"technique": "gate"}),)
    usage = reply.usage
    assert (usage.prompt_tokens, usage.completion_tokens) == (40, 20)
    assert (usage.cache_write_5m_tokens, usage.cache_write_1h_tokens) == (0, 500)
    assert usage.cache_read_tokens == 3000


@pytest.mark.parametrize(
    ("stop_reason", "match"), [("refusal", "declined"), ("max_tokens", "cut off")]
)
def test_unusable_replies_are_errors(stop_reason: str, match: str) -> None:
    client = AnthropicClient("m", client=Stub(message([], stop_reason=stop_reason)))
    with pytest.raises(ProviderError, match=match):
        client.chat([Message(role="user", content="hi")], None, GenParams())


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (status_error(anthropic.RateLimitError, 429), TransientProviderError),
        (status_error(anthropic.InternalServerError, 500), TransientProviderError),
        (status_error(anthropic.InternalServerError, 529), TransientProviderError),
        (
            status_error(anthropic.BadRequestError, 400, "prompt is too long: 1200000"),
            ContextLengthError,
        ),
        (status_error(anthropic.BadRequestError, 400, "invalid tool schema"), ProviderError),
        (status_error(anthropic.AuthenticationError, 401), ProviderError),
        (
            anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")),
            TransientProviderError,
        ),
        (
            anthropic.APITimeoutError(request=httpx2.Request("POST", "https://x")),
            TransientProviderError,
        ),
    ],
)
def test_errors_are_classified(error: anthropic.AnthropicError, kind: type) -> None:
    assert type(classify_error(error)) is kind


def test_transient_errors_are_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry.time, "sleep", lambda _: None)
    stub = Stub(
        status_error(anthropic.RateLimitError, 429),
        status_error(anthropic.InternalServerError, 503),
        message([{"type": "text", "text": "ok"}]),
    )
    reply = AnthropicClient("m", client=stub).chat(
        [Message(role="user", content="hi")], None, GenParams()
    )
    assert reply.text == "ok"
    assert len(stub.requests) == 3


def test_final_errors_are_not_retried() -> None:
    stub = Stub(status_error(anthropic.BadRequestError, 400, "bad"))
    with pytest.raises(ProviderError, match="400"):
        AnthropicClient("m", client=stub).chat(
            [Message(role="user", content="hi")], None, GenParams()
        )
    assert len(stub.requests) == 1


def test_a_missing_key_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", "/nonexistent")
    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
        AnthropicClient("m")


def test_credentials_missing_at_request_time_are_a_clear_error() -> None:
    # the SDK may build a client without credentials and fail only when it sends a request
    stub = Stub(TypeError('"Could not resolve authentication method. Expected one of api_key'))
    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
        AnthropicClient("m", client=stub).chat(
            [Message(role="user", content="hi")], None, GenParams()
        )


def test_other_type_errors_are_not_hidden() -> None:
    stub = Stub(TypeError("unexpected keyword argument 'fallbacks'"))
    with pytest.raises(TypeError, match="fallbacks"):
        AnthropicClient("m", client=stub).chat(
            [Message(role="user", content="hi")], None, GenParams()
        )
