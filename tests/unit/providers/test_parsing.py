import contextlib
import sys

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
        ('Sure! {"tool": "read_file", "args": {"path": "a.py",},} done', True),
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


WRITE = ToolSpec(
    name="write_file",
    description="Write a file",
    parameters={"type": "object", "properties": {"content": {"type": "string"}}},
)


def test_commas_inside_strings_are_preserved() -> None:
    text = '{"tool": "write_file", "args": {"content": "x = {a: 1, }; y = [1, ]"}}'
    _, args, repaired = parse_json_tool_call(text, [WRITE])
    assert args["content"] == "x = {a: 1, }; y = [1, ]"
    assert repaired is False


def test_trailing_comma_removal_counts_as_repair() -> None:
    _, args, repaired = parse_json_tool_call(
        '{"tool": "write_file", "args": {"content": "a, }",},}', [WRITE]
    )
    assert args["content"] == "a, }"
    assert repaired is True


def test_backticks_inside_a_fenced_string() -> None:
    text = '```json\n{"tool": "write_file", "args": {"content": "use ``` fences"}}\n````\n'
    _, args, _ = parse_json_tool_call(text, [WRITE])
    assert args["content"] == "use ``` fences"


def test_falls_back_to_full_text_when_fence_has_no_object() -> None:
    text = '```\nls -la\n```\n{"tool": "write_file", "args": {"content": "ok"}}'
    _, args, _ = parse_json_tool_call(text, [WRITE])
    assert args["content"] == "ok"


def test_python_escapes_are_decoded_on_repair() -> None:
    value, repaired = extract_json_object(r"{'content': 'line1\nline2\ttab'}")
    assert value == {"content": "line1\nline2\ttab"}
    assert repaired


@pytest.mark.parametrize(
    "text",
    [
        '{"tool": "write_file", "args": {"n": ' + "9" * 5000 + "}}",
        '{"tool": "write_file", "args": ' + "[" * 100_000 + "]" * 100_000 + "}",
    ],
    ids=["huge-integer", "deep-nesting"],
)
def test_pathological_numbers_and_nesting_are_malformed(text: str) -> None:
    # Pin the interpreter's int-conversion limit; PYTHONINTMAXSTRDIGITS=0 would disable it.
    saved = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        with pytest.raises(MalformedToolCall):
            parse_json_tool_call(text, [WRITE])
    finally:
        sys.set_int_max_str_digits(saved)


@pytest.mark.parametrize(
    "text",
    [
        '{not_json}\n~~~json\n{"tool": "write_file", "args": {"content": "ok"}}\n~~~~\n',
        '{not_json}\n   ```json\n{"tool": "write_file", "args": {"content": "ok"}}\n   ```\n',
    ],
    ids=["tilde-fence", "indented-fence"],
)
def test_tilde_and_indented_fences(text: str) -> None:
    _, args, _ = parse_json_tool_call(text, [WRITE])
    assert args["content"] == "ok"


def test_closing_fence_must_use_the_same_character() -> None:
    # A ~~~ line cannot close a backtick fence, so the JSON after it is still inside.
    text = '{not_json}\n```\n~~~\n{"tool": "write_file", "args": {"content": "ok"}}\n```\n'
    _, args, _ = parse_json_tool_call(text, [WRITE])
    assert args["content"] == "ok"
