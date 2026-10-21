import logging
from types import SimpleNamespace
from typing import Any

import openai
import pytest

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
from saddlefit.providers.openai_compat import (
    OpenAICompatibleClient,
    map_error,
    to_openai_messages,
)

READ = ToolSpec(
    name="read_file",
    description="Read a file",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
)
USER = [Message(role="user", content="go")]


def response(content: str | None = None, tool_calls: list[Any] | None = None) -> Any:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def native_call(args: str, call_id: str = "c1") -> Any:
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name="read_file", arguments=args))


class StubSDK:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def status_error(cls: type[openai.APIStatusError], status: int, message: str) -> Exception:
    exc = cls.__new__(cls)
    Exception.__init__(exc, message)
    exc.status_code = status
    return exc


def connection_error() -> Exception:
    exc = openai.APIConnectionError.__new__(openai.APIConnectionError)
    Exception.__init__(exc, "connection reset")
    return exc


def client(sdk: StubSDK, fetch: Any = None) -> OpenAICompatibleClient:
    def no_runner(url: str, payload: Any) -> Any:
        raise OSError("not ollama")

    return OpenAICompatibleClient(
        base_url="http://localhost:11434/v1",
        model="qwen3:8b",
        sdk=sdk,
        fetch_json=fetch or no_runner,
        sleep=lambda _: None,
    )


def test_satisfies_protocol() -> None:
    assert isinstance(client(StubSDK()), ModelClient)


def test_native_tool_calls_and_usage() -> None:
    sdk = StubSDK(response(tool_calls=[native_call('{"path": "a.py",}')]))
    reply = client(sdk).chat(USER, [READ], GenParams(seed=8, max_tokens=50))
    assert reply.tool_calls == (ToolCall(id="c1", name="read_file", arguments={"path": "a.py"}),)
    assert (reply.usage.prompt_tokens, reply.usage.completion_tokens) == (11, 7)
    sent = sdk.requests[0]
    assert sent["tools"][0]["function"]["name"] == "read_file"
    assert (sent["seed"], sent["max_tokens"]) == (8, 50)
    assert "response_format" not in sent


def test_native_malformed_arguments_are_reported() -> None:
    sdk = StubSDK(response(tool_calls=[native_call("not json")]))
    reply = client(sdk).chat(USER, [READ], GenParams())
    assert reply.tool_calls == ()
    assert reply.malformed == ("not json",)


def test_json_mode_uses_constrained_output_and_parses() -> None:
    sdk = StubSDK(response(content="{'tool': 'read_file', 'args': {'path': 'b.py'}}"))
    reply = client(sdk).chat(USER, [READ], GenParams(tool_mode="json"))
    assert reply.tool_calls[0].arguments == {"path": "b.py"}
    assert reply.repaired
    sent = sdk.requests[0]
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["messages"][0]["role"] == "system"
    assert "tools" not in sent


def test_json_mode_garbage_is_malformed() -> None:
    reply = client(StubSDK(response(content="I will read it"))).chat(
        USER, [READ], GenParams(tool_mode="json")
    )
    assert reply.malformed == ("I will read it",)


def test_transient_errors_are_retried() -> None:
    sdk = StubSDK(
        status_error(openai.RateLimitError, 429, "slow down"),
        connection_error(),
        response(content="done"),
    )
    assert client(sdk).chat(USER, None, GenParams()).text == "done"
    assert len(sdk.requests) == 3


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (status_error(openai.InternalServerError, 503, "busy"), TransientProviderError),
        (
            status_error(openai.BadRequestError, 400, "exceeds the context length"),
            ContextLengthError,
        ),
        (status_error(openai.AuthenticationError, 401, "bad key"), ProviderError),
    ],
)
def test_error_mapping(exc: Exception, kind: type[ProviderError]) -> None:
    assert type(map_error(exc)) is kind


def test_permanent_errors_are_not_retried() -> None:
    sdk = StubSDK(status_error(openai.BadRequestError, 400, "bad request"))
    with pytest.raises(ProviderError):
        client(sdk).chat(USER, None, GenParams())
    assert len(sdk.requests) == 1


def test_info_reads_ollama_digest_and_version() -> None:
    def fetch(url: str, payload: Any) -> Any:
        if url == "http://localhost:11434/api/version":
            return {"version": "0.40.6"}
        assert url == "http://localhost:11434/api/tags"
        return {"models": [{"name": "other"}, {"name": "qwen3:8b", "digest": "500a1f"}]}

    info = client(StubSDK(), fetch).info()
    assert (info.runner, info.runner_version, info.digest) == ("ollama", "0.40.6", "sha256:500a1f")


def test_info_without_ollama_api() -> None:
    info = client(StubSDK()).info()
    assert (info.runner, info.digest) == ("openai-compatible", None)


def test_message_conversion() -> None:
    call = ToolCall(id="c1", name="read_file", arguments={"path": "a"})
    out = to_openai_messages(
        [
            Message(role="assistant", tool_calls=(call,)),
            Message(role="tool", content="text", tool_call_id="c1"),
        ]
    )
    assert out[0]["tool_calls"][0]["function"] == {
        "name": "read_file",
        "arguments": '{"path": "a"}',
    }
    assert out[1] == {"role": "tool", "content": "text", "tool_call_id": "c1"}


def named_call(name: Any, args: str, call_id: str = "c9") -> Any:
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=args))


@pytest.mark.parametrize(
    "call",
    [
        named_call("rm_rf", '{"path": "/"}'),
        named_call(None, '{"path": "a"}'),
        named_call("read_file", "{}"),
        named_call("read_file", '{"path": 3}'),
    ],
    ids=["undeclared-tool", "missing-name", "missing-required-arg", "wrong-arg-type"],
)
def test_native_calls_are_validated_against_declared_tools(call: Any) -> None:
    sdk = StubSDK(response(tool_calls=[call, native_call('{"path": "ok.py"}')]))
    reply = client(sdk).chat(USER, [READ], GenParams())
    assert [c.arguments for c in reply.tool_calls] == [{"path": "ok.py"}]
    assert len(reply.malformed) == 1


def ollama(url: str, payload: Any) -> Any:
    return {"version": "0.40.6"} if url.endswith("/api/version") else {"models": []}


@pytest.mark.parametrize(
    ("effort", "sent_value"), [("off", "none"), ("low", "low"), ("high", "high"), (None, None)]
)
def test_reasoning_effort_reaches_ollama(effort: str | None, sent_value: str | None) -> None:
    sdk = StubSDK(response(content="ok"))
    client(sdk, fetch=ollama).chat(USER, None, GenParams(reasoning=effort))  # type: ignore[arg-type]
    sent = sdk.requests[-1]
    assert sent.get("reasoning_effort") == sent_value
    assert ("reasoning_effort" in sent) is (effort is not None)


@pytest.mark.parametrize(
    ("effort", "sent_value"), [("off", None), ("low", "low"), ("high", "high")]
)
def test_other_runners_get_only_standard_effort_values(effort: str, sent_value: str | None) -> None:
    # "none" is Ollama's own value; elsewhere "off" sends nothing rather than risk a rejection
    sdk = StubSDK(response(content="ok"))
    client(sdk).chat(USER, None, GenParams(reasoning=effort))  # type: ignore[arg-type]
    assert sdk.requests[-1].get("reasoning_effort") == sent_value


def test_off_that_cannot_be_applied_is_reported_once(caplog: pytest.LogCaptureFixture) -> None:
    sdk = StubSDK(response(content="ok"), response(content="ok"))
    student = client(sdk)
    with caplog.at_level(logging.WARNING, logger="saddlefit.providers.openai_compat"):
        student.chat(USER, None, GenParams(reasoning="off"))
        student.chat(USER, None, GenParams(reasoning="off"))
    warnings = [r for r in caplog.records if "reasoning" in r.getMessage()]
    assert len(warnings) == 1
    assert "not applied" in warnings[0].getMessage()
