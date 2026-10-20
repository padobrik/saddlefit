"""Required test 9: secrets never appear in logs."""

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from saddlefit.logs import REDACTED, configure_logging, redact

ANTHROPIC = "sk-ant-api03-" + "A1b2C3d4" * 6
OPENAI = "sk-proj-" + "Z9y8X7w6" * 5
GITHUB = "ghp_" + "q" * 36
CUSTOM = "plain-looking-secret-value"


@pytest.fixture(autouse=True)
def restore_logger() -> Iterator[None]:
    logger = logging.getLogger("saddlefit")
    saved = (list(logger.handlers), logger.level, logger.propagate)
    yield
    logger.handlers, logger.level, logger.propagate = saved


@pytest.fixture
def log_stream(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC)
    monkeypatch.setenv("HF_TOKEN", CUSTOM)
    stream = io.StringIO()
    configure_logging(stream)
    return stream


@pytest.mark.parametrize(
    "secret",
    [ANTHROPIC, OPENAI, GITHUB, "AKIA" + "ABCDEFGHIJKLMNOP", "hf_" + "x" * 30],
)
def test_key_shapes_are_redacted(secret: str) -> None:
    assert redact(f"key is {secret}!", env={}) == f"key is {REDACTED}!"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", f"Authorization: Bearer {REDACTED}"),
        ('{"api_key": "abcdefgh12345"}', f'{{"api_key": "{REDACTED}"}}'),
        ("x-api-key=abcdefgh12345", f"x-api-key={REDACTED}"),
        ('api_key="abcdefgh topsecret" next', f'api_key="{REDACTED}" next'),
        ("api_key='abcdefgh topsecret', x", f"api_key='{REDACTED}', x"),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", f"Authorization: Basic {REDACTED}"),
        ('authorization="Token abcdefghijklmnop" end', f'authorization="Token {REDACTED}" end'),
        ("header Basic dXNlcjpwYXNzd29yZA== sent", f"header Basic {REDACTED} sent"),
    ],
)
def test_key_assignments_are_redacted(text: str, expected: str) -> None:
    assert redact(text, env={}) == expected


def test_known_env_values_are_redacted_even_without_key_shape() -> None:
    assert redact(f"token {CUSTOM}", env={"HF_TOKEN": CUSTOM}) == f"token {REDACTED}"
    assert redact("short", env={"HF_TOKEN": "short"}) == "short"


def test_ordinary_text_is_untouched() -> None:
    text = "model qwen3:8b solved 4/6 held-out tasks; sha256:abc123 run 01JB2Y8Q4V9N1K3D7M5R6T0W2X"
    assert redact(text, env={}) == text


def test_secrets_never_reach_log_output(log_stream: io.StringIO) -> None:
    log = logging.getLogger("saddlefit.providers")
    log.info("calling tutor with key %s", ANTHROPIC)
    log.warning("headers", extra={"headers": {"x-api-key": ANTHROPIC, "auth": [OPENAI]}})
    log.error("custom %s", CUSTOM)
    try:
        raise RuntimeError(f"401 for key {ANTHROPIC}")
    except RuntimeError:
        log.exception("tutor call failed")

    output = log_stream.getvalue()
    for secret in (ANTHROPIC, OPENAI, CUSTOM):
        assert secret not in output
    lines = [json.loads(line) for line in output.splitlines()]
    assert [line["level"] for line in lines] == ["info", "warning", "error", "error"]
    assert lines[0]["message"] == f"calling tutor with key {REDACTED}"
    assert lines[1]["headers"] == {"x-api-key": REDACTED, "auth": [REDACTED]}
    assert "401 for key [REDACTED]" in lines[3]["exception"]


def test_plain_console_format(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    configure_logging(stream, json_lines=False)
    logging.getLogger("saddlefit").info("hello %s", GITHUB)
    assert stream.getvalue() == f"hello {REDACTED}\n"


def test_mapping_keys_and_objects_are_redacted(log_stream: io.StringIO) -> None:
    extras: dict[str, Any] = {
        "headers": {CUSTOM: "present", "nested": {ANTHROPIC: [1, 2.5, True, None]}},
        "error": RuntimeError(f"failed with {CUSTOM}"),
        "count": 3,
    }
    logging.getLogger("saddlefit").info("call", extra=extras)
    output = log_stream.getvalue()
    for secret in (ANTHROPIC, CUSTOM):
        assert secret not in output
    line = json.loads(output)
    assert line["headers"] == {REDACTED: "present", "nested": {REDACTED: [1, 2.5, True, None]}}
    assert line["error"] == f"failed with {REDACTED}"
    assert line["count"] == 3
