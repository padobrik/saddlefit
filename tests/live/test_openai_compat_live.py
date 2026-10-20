import os

import pytest

from saddlefit.providers.base import GenParams, Message, ToolSpec
from saddlefit.providers.openai_compat import OpenAICompatibleClient

pytestmark = pytest.mark.live

BASE_URL = os.environ.get("SADDLEFIT_LIVE_BASE_URL", "http://localhost:11434/v1")
MODEL = os.environ.get("SADDLEFIT_LIVE_MODEL", "qwen3:0.6b")
FINISH = ToolSpec(
    name="finish",
    description="End the task with a short summary",
    parameters={
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
    },
)


def test_live_info_and_tool_call() -> None:
    client = OpenAICompatibleClient(base_url=BASE_URL, model=MODEL)
    info = client.info()
    assert info.runner == "ollama"
    assert info.digest
    assert info.digest.startswith("sha256:")
    messages = [Message(role="user", content="Call the finish tool with summary 'done'. /no_think")]
    reply = client.chat(messages, [FINISH], GenParams(tool_mode="json", seed=7, temperature=0))
    assert reply.usage.prompt_tokens > 0
    assert [c.name for c in reply.tool_calls] == ["finish"]
