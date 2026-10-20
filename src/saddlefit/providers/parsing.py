"""Tolerant parsing of tool calls in ``json`` tool mode (B5).

The model answers with one object, ``{"tool": "...", "args": {...}}``. Extraction tolerates
code fences, surrounding prose and trailing commas. When strict parsing fails, one repair
pass rewrites Python-style literals (single quotes, ``True``/``False``/``None``).
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from saddlefit.providers.base import ToolSpec

# CommonMark fences: ``` or ~~~, indented up to three spaces. The closer uses the same
# character and is at least as long as the opener.
_FENCE = re.compile(
    r"^ {0,3}(?P<fence>(?P<ch>[`~])(?P=ch){2,})[^\n]*\n"
    r"(?P<body>.*?)"
    r"^ {0,3}(?P=fence)(?P=ch)*[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
_STRING_TOKEN = re.compile(r"(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')")
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


def _outside_strings(candidate: str, fix: Callable[[str], str]) -> str:
    """Apply ``fix`` only to the parts of ``candidate`` that are not string literals."""
    return "".join(
        token if _STRING_TOKEN.fullmatch(token) else fix(token)
        for token in _STRING_TOKEN.split(candidate)
    )


def _strip_trailing_commas(candidate: str) -> str:
    return _outside_strings(candidate, lambda part: _TRAILING_COMMA.sub(r"\1", part))


def _single_quoted_to_json(token: str) -> str:
    try:
        value = ast.literal_eval(token)
    except (SyntaxError, ValueError):
        value = token[1:-1].replace('\\"', '"').replace("\\'", "'")
    return json.dumps(value if isinstance(value, str) else str(value))


def _repair(candidate: str) -> str:
    """Rewrite Python-style literals: single-quoted strings and True/False/None."""
    out: list[str] = []
    for token in _STRING_TOKEN.split(candidate):
        if token.startswith("'") and _STRING_TOKEN.fullmatch(token):
            out.append(_single_quoted_to_json(token))
        elif token.startswith('"') and _STRING_TOKEN.fullmatch(token):
            out.append(token)
        else:
            out.append(re.sub(r"\b(True|False|None)\b", lambda m: _PY_LITERALS[m[1]], token))
    return "".join(out)


def _loads(candidate: str) -> Any:
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise MalformedToolCall(f"invalid JSON: {exc.msg}") from exc
    except (ValueError, RecursionError) as exc:
        raise MalformedToolCall(f"invalid JSON: {type(exc).__name__}") from exc


def _find_candidate(text: str) -> str | None:
    for fenced in _FENCE.finditer(text):
        candidate = _first_object(fenced.group("body"))
        if candidate is not None:
            return candidate
    return _first_object(text)


def extract_json_object(text: str) -> tuple[dict[str, Any], bool]:
    """Extract one JSON object from model text; return it and whether repair was needed.

    Valid JSON is parsed unchanged. Otherwise trailing commas outside strings are dropped,
    and then Python-style literals are rewritten; either step counts as a repair.
    """
    candidate = _find_candidate(text)
    if candidate is None:
        raise MalformedToolCall("no JSON object found")
    attempts = (
        (candidate, False),
        (_strip_trailing_commas(candidate), True),
        (_strip_trailing_commas(_repair(candidate)), True),
    )
    errors: list[MalformedToolCall] = []
    for attempt, repaired in attempts:
        try:
            value = _loads(attempt)
        except MalformedToolCall as exc:
            errors.append(exc)
            continue
        if not isinstance(value, dict):
            raise MalformedToolCall("top-level JSON value is not an object")
        return value, repaired
    raise errors[0]


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
