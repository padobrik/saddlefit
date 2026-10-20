"""Structured JSON-line logging with secret redaction (B5, B15).

Every record passes through ``RedactingFilter``: anything shaped like an API key, and the
exact values of known secret environment variables, are replaced before formatting.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

REDACTED = "[REDACTED]"

SECRET_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
)

_KEY_SHAPES = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"hf_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]

_KEY_NAME = r"(?:api[_-]?key|x-api-key|authorization)[\"']?\s*[:=]\s*"
_SCHEME = r"(?:bearer|basic|token|digest)\s+"
# api_key="a b c": the whole quoted value, keeping an auth scheme word if present
_QUOTED_ASSIGNMENT = re.compile(rf"(?i)({_KEY_NAME})([\"'])({_SCHEME})?(?:(?!\2).)+\2")
# Authorization: Basic xxx, or a bare "Bearer xxx" anywhere
_SCHEME_VALUE = re.compile(rf"(?i)\b({_SCHEME})[A-Za-z0-9._~+/=\-]{{8,}}")
# api_key=xxx without quotes and without a scheme
_BARE_ASSIGNMENT = re.compile(rf"(?i)({_KEY_NAME})(?![\"'])(?!{_SCHEME})[^\s\"',;]{{8,}}")


def _redact_quoted(match: re.Match[str]) -> str:
    prefix, quote, scheme = match.group(1), match.group(2), match.group(3) or ""
    return f"{prefix}{quote}{scheme}{REDACTED}{quote}"


_MIN_SECRET_LEN = 8


def _secret_values(env: Mapping[str, str]) -> list[str]:
    values = [env[name] for name in SECRET_ENV_VARS if len(env.get(name, "")) >= _MIN_SECRET_LEN]
    return sorted(values, key=len, reverse=True)


def redact(text: str, env: Mapping[str, str] | None = None) -> str:
    """Replace key-shaped substrings and known secret values with ``[REDACTED]``."""
    for value in _secret_values(os.environ if env is None else env):
        text = text.replace(value, REDACTED)
    for pattern in _KEY_SHAPES:
        text = pattern.sub(REDACTED, text)
    text = _QUOTED_ASSIGNMENT.sub(_redact_quoted, text)
    text = _SCHEME_VALUE.sub(lambda m: m.group(1) + REDACTED, text)
    return _BARE_ASSIGNMENT.sub(lambda m: m.group(1) + REDACTED, text)


def _redact_value(value: Any) -> Any:
    """Redact strings, mapping keys and values, and the text of any other object."""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {_redact_value(k): _redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(v) for v in value]
    # Anything else would reach json.dumps(default=str) unredacted.
    return redact(str(value))


class RedactingFilter(logging.Filter):
    """Redact the message, its arguments, extra fields and exception text of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        for key in _extra_keys(record):
            setattr(record, key, _redact_value(getattr(record, key)))
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


_STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


def _extra_keys(record: logging.LogRecord) -> Iterable[str]:
    return [k for k in vars(record) if k not in _STANDARD]


class JsonLineFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message, extras and exception."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update({k: getattr(record, k) for k in _extra_keys(record)})
        if record.exc_text:
            entry["exception"] = record.exc_text
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging(
    stream: Any = None, *, json_lines: bool = True, level: int = logging.INFO
) -> None:
    """Route the ``saddlefit`` logger through redaction to ``stream`` (stderr by default)."""
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonLineFormatter() if json_lines else logging.Formatter("%(message)s"))
    logger = logging.getLogger("saddlefit")
    logger.handlers = [handler]
    logger.setLevel(level)
    logger.propagate = False
