"""Tolerant parsing of tool calls in ``json`` tool mode (B5).

The model answers with one object, ``{"tool": "...", "args": {...}}``. Extraction tolerates
code fences, surrounding prose and trailing commas. When strict parsing fails, one repair
pass rewrites Python-style literals (single quotes, ``True``/``False``/``None``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from saddlefit.providers.base import ToolSpec

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_PY_LITERALS = {"True": "true", "False": "false", "None": "null"}


class MalformedToolCall(ValueError):
    """The reply holds no valid tool call, even after repair."""


def _first_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` span, respecting strings."""
    start = text.find("{")
    while start != -1:
        depth, in_str, quote, escaped = 0, False, "", False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote:
                    in_str = False
            elif ch in "\"'":
                in_str, quote = True, ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        start = text.find("{", start + 1)
    return None


def _repair(candidate: str) -> str:
    out: list[str] = []
    for token in re.split(r"(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')", candidate):
        if token.startswith("'") and token.endswith("'") and len(token) >= 2:
            inner = token[1:-1].replace('\\"', '"').replace("\\'", "'")
            out.append(json.dumps(inner))
        elif token.startswith('"'):
            out.append(token)
        else:
            out.append(re.sub(r"\b(True|False|None)\b", lambda m: _PY_LITERALS[m[1]], token))
    return "".join(out)


def extract_json_object(text: str) -> tuple[dict[str, Any], bool]:
    """Extract one JSON object from model text; return it and whether repair was needed."""
    fenced = _FENCE.search(text)
    body = fenced.group(1) if fenced else text
    candidate = _first_object(body)
    if candidate is None:
        raise MalformedToolCall("no JSON object found")
    candidate = _TRAILING_COMMA.sub(r"\1", candidate)
    try:
        value = json.loads(candidate)
        repaired = False
    except json.JSONDecodeError:
        try:
            value = json.loads(_TRAILING_COMMA.sub(r"\1", _repair(candidate)))
        except json.JSONDecodeError as exc:
            raise MalformedToolCall(f"invalid JSON: {exc.msg}") from exc
        repaired = True
    if not isinstance(value, dict):
        raise MalformedToolCall("top-level JSON value is not an object")
    return value, repaired


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def schema_errors(value: Any, schema: dict[str, Any], path: str = "args") -> list[str]:
    """Validate against the JSON Schema subset used by tool parameters."""
    errors: list[str] = []
    expected = schema.get("type")
    if isinstance(expected, str) and expected in _JSON_TYPES:
        ok = isinstance(value, _JSON_TYPES[expected])
        if expected in {"integer", "number"} and isinstance(value, bool):
            ok = False
        if not ok:
            return [f"{path}: expected {expected}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")
    if isinstance(value, dict):
        props: dict[str, Any] = schema.get("properties", {})
        errors += [f"{path}.{k}: required" for k in schema.get("required", []) if k not in value]
        for key, item in value.items():
            if key in props:
                errors += schema_errors(item, props[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: unknown argument")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            errors += schema_errors(item, schema["items"], f"{path}[{i}]")
    return errors


def parse_json_tool_call(text: str, tools: Sequence[ToolSpec]) -> tuple[str, dict[str, Any], bool]:
    """Parse ``{"tool": ..., "args": {...}}``; return name, args and whether it was repaired."""
    obj, repaired = extract_json_object(text)
    name = obj.get("tool")
    args = obj.get("args", {})
    by_name = {t.name: t for t in tools}
    if not isinstance(name, str) or name not in by_name:
        raise MalformedToolCall(f"unknown tool {name!r}; expected one of {sorted(by_name)}")
    if not isinstance(args, dict):
        raise MalformedToolCall("args must be an object")
    errors = schema_errors(args, by_name[name].parameters)
    if errors:
        raise MalformedToolCall("; ".join(errors))
    return name, args, repaired


def json_mode_schema(tools: Sequence[ToolSpec]) -> dict[str, Any]:
    """The JSON Schema a ``json``-mode reply must satisfy, for schema-constrained runners."""
    return {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "properties": {"tool": {"const": t.name}, "args": t.parameters},
                "required": ["tool", "args"],
                "additionalProperties": False,
            }
            for t in tools
        ],
    }
