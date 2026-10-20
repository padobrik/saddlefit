import contextlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from saddlefit.providers.base import ToolSpec
from saddlefit.providers.parsing import (
    MalformedToolCall,
    extract_json_object,
    json_mode_schema,
    parse_json_tool_call,
    schema_errors,
)

READ = ToolSpec(
    name="read_file",
    description="Read a file",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}, "start": {"type": "integer"}},
        "required": ["path"],
        "additionalProperties": False,
    },
)


@pytest.mark.parametrize(
    ("text", "repaired"),
    [
        ('{"tool": "read_file", "args": {"path": "a.py"}}', False),
        ('```json\n{"tool": "read_file", "args": {"path": "a.py"}}\n```', False),
        ('Sure! {"tool": "read_file", "args": {"path": "a.py",},} done', False),
        ("{'tool': 'read_file', 'args': {'path': 'a.py'}}", True),
        ('{"tool": "read_file", "args": {"path": "it\'s {here}.py"}}', False),
    ],
)
def test_tolerant_extraction(text: str, repaired: bool) -> None:
    name, args, was_repaired = parse_json_tool_call(text, [READ])
    assert name == "read_file"
    assert args["path"].endswith(".py")
    assert was_repaired is repaired


def test_python_literals_are_repaired() -> None:
    value, repaired = extract_json_object("{'a': True, 'b': None, 'c': 'True'}")
    assert value == {"a": True, "b": None, "c": "True"}
    assert repaired


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("no json here", "no JSON object"),
        ('{"tool": "read_file", "args": {"path": }', "invalid JSON"),
        ('{"tool": "read_file" "args": 1}', "invalid JSON"),
        ('{"tool": "rm_rf", "args": {}}', "unknown tool"),
        ('{"tool": "read_file", "args": []}', "args must be an object"),
        ('{"tool": "read_file", "args": {}}', "args.path: required"),
        ('{"tool": "read_file", "args": {"path": 3}}', "args.path: expected string"),
        ('{"tool": "read_file", "args": {"path": "a", "x": 1}}', "unknown argument"),
        ('{"tool": "read_file", "args": {"path": "a", "start": true}}', "expected integer"),
    ],
)
def test_malformed_calls(text: str, message: str) -> None:
    with pytest.raises(MalformedToolCall, match=message):
        parse_json_tool_call(text, [READ])


def test_top_level_must_be_object() -> None:
    with pytest.raises(MalformedToolCall):
        extract_json_object("[1, 2]")


def test_schema_subset_enum_and_items() -> None:
    schema = {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}}
    assert schema_errors(["a", "b"], schema) == []
    assert schema_errors(["a", "c"], schema) == ["args[1]: must be one of ['a', 'b']"]


def test_json_mode_schema_lists_each_tool() -> None:
    schema = json_mode_schema([READ])
    assert schema["oneOf"][0]["properties"]["tool"] == {"const": "read_file"}


@given(st.text())
def test_parser_never_raises_unexpected_errors(text: str) -> None:
    with contextlib.suppress(MalformedToolCall):
        parse_json_tool_call(text, [READ])
