from pathlib import Path

import pytest

from saddlefit.providers.base import (
    GenParams,
    Message,
    ModelClient,
    ModelReply,
    ProviderError,
    ToolSpec,
)
from saddlefit.providers.fake import FakeClient

FINISH = ToolSpec(
    name="finish",
    description="End the task",
    parameters={"type": "object", "properties": {"summary": {"type": "string"}}},
)
USER = [Message(role="user", content="fix it")]
JSON = GenParams(tool_mode="json")


def test_satisfies_protocol() -> None:
    assert isinstance(FakeClient([]), ModelClient)


def test_replays_in_order_and_records_calls() -> None:
    client = FakeClient([ModelReply(text="one"), "two"])
    assert client.chat(USER, None, GenParams()).text == "one"
    assert client.chat(USER, None, GenParams()).text == "two"
    assert len(client.calls) == 2
    with pytest.raises(ProviderError, match="no scripted replies"):
        client.chat(USER, None, GenParams())


def test_json_mode_text_is_parsed() -> None:
    client = FakeClient(['{"tool": "finish", "args": {"summary": "done"}}', "garbage"])
    reply = client.chat(USER, [FINISH], JSON)
    assert reply.tool_calls[0].name == "finish"
    assert reply.tool_calls[0].arguments == {"summary": "done"}
    bad = client.chat(USER, [FINISH], JSON)
    assert bad.tool_calls == ()
    assert bad.malformed == ("garbage",)


def test_script_function() -> None:
    client = FakeClient(lambda messages, tools, params: f"seen {len(messages)}")
    assert client.chat(USER * 2, None, GenParams()).text == "seen 2"


def test_from_fixture(tmp_path: Path) -> None:
    path = tmp_path / "replies.jsonl"
    path.write_text('"plain"\n\n{"text": "structured", "repaired": true}\n', encoding="utf-8")
    client = FakeClient.from_fixture(path, model="m")
    assert client.info().model == "m"
    assert client.chat(USER, None, GenParams()).text == "plain"
    assert client.chat(USER, None, GenParams()).repaired
