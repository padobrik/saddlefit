import base64
import shlex
from dataclasses import dataclass, field

import pytest

from saddlefit.sandbox.container import ExecResult
from saddlefit.tools.builtin import (
    MAX_COMMAND_TIMEOUT_S,
    TOOL_NAMES,
    TOOL_SPECS,
    Toolbox,
    ToolError,
    confine,
    truncate,
)


@dataclass
class FakeSandbox:
    replies: list[ExecResult] = field(default_factory=list)
    commands: list[tuple[str, float]] = field(default_factory=list)

    def exec(self, command: str, timeout_s: float) -> ExecResult:
        self.commands.append((command, timeout_s))
        if self.replies:
            return self.replies.pop(0)
        return ExecResult(exit_code=0, output="ok", timed_out=False, duration_s=0.1)


@pytest.mark.parametrize(
    ("path", "expected"),
    [("src/a.py", "/work/src/a.py"), ("/work/x", "/work/x"), (".", "/work"), ("a/../b", "/work/b")],
)
def test_confine_inside(path: str, expected: str) -> None:
    assert confine(path) == expected


@pytest.mark.parametrize("path", ["../etc/passwd", "/etc/passwd", "/workshop/x", "", "a\x00b"])
def test_confine_refuses_escapes(path: str) -> None:
    with pytest.raises(ToolError):
        confine(path)


def test_truncate_keeps_head_and_tail() -> None:
    assert truncate("short", 10) == "short"
    out = truncate("a" * 50 + "b" * 50, 20)
    assert out.startswith("a" * 10)
    assert out.endswith("b" * 10)
    assert "80 characters cut" in out


def test_commands_quote_paths_and_patterns() -> None:
    box = Toolbox(FakeSandbox())
    box.read_file("my file.py")
    box.read_file("a.py", start=3, end=7)
    box.list_dir()
    box.search("x; rm -rf /", "src")
    commands = [c for c, _ in box.sandbox.commands]  # type: ignore[attr-defined]
    assert commands[0] == "cat -n -- '/work/my file.py'"
    assert commands[1] == "cat -n -- /work/a.py | sed -n '3,7p'"
    assert commands[2] == "ls -1Ap -- /work"
    assert commands[3] == "rg -n --max-columns 300 -e 'x; rm -rf /' -- /work/src"


def test_edit_and_write_send_content_as_base64() -> None:
    sandbox = FakeSandbox()
    box = Toolbox(sandbox)
    box.edit_file("a.py", "old'; rm -rf / #", "new")
    argv = shlex.split(sandbox.commands[0][0])
    assert argv[:2] == ["python3", "-c"]
    assert argv[3] == "edit"
    assert [base64.b64decode(a).decode() for a in argv[4:]] == [
        "/work/a.py",
        "old'; rm -rf / #",
        "new",
    ]
    box.write_file("new/b.py", "print('hi')\n")
    argv = shlex.split(sandbox.commands[1][0])
    assert argv[3] == "write"


def test_results_reflect_exit_codes_timeouts_and_caps() -> None:
    sandbox = FakeSandbox(
        [
            ExecResult(exit_code=1, output="boom", timed_out=False, duration_s=0.1),
            ExecResult(exit_code=0, output="x" * 100, timed_out=True, duration_s=9),
        ]
    )
    box = Toolbox(sandbox, max_chars=40)
    assert box.bash("false").ok is False
    slow = box.bash("sleep 99", timeout=5)
    assert slow.ok is False
    assert "characters cut" in slow.output
    assert sandbox.commands[1][1] == 5


def test_call_dispatch_and_argument_errors() -> None:
    box = Toolbox(FakeSandbox())
    assert box.call("list_dir", {}).ok
    assert box.call("finish", {"summary": "done"}).finished
    assert box.call("rm_rf", {}).output == "unknown tool: rm_rf"
    assert box.call("call", {}).output == "unknown tool: call"
    bad = box.call("read_file", {"path": "a", "colour": "red"})
    assert (bad.ok, bad.output.startswith("error: bad arguments for read_file")) == (False, True)
    escape = box.call("read_file", {"path": "../../etc/passwd"})
    assert (escape.ok, "outside the workspace" in escape.output) == (False, True)
    assert box.call("edit_file", {"path": "a", "old": "", "new": "x"}).ok is False
    assert box.call("bash", {"command": "  "}).ok is False
    assert box.call("write_file", {"path": "a", "content": "x" * 600_000}).ok is False


def test_specs_cover_every_tool() -> None:
    assert {
        "read_file",
        "list_dir",
        "search",
        "edit_file",
        "write_file",
        "bash",
        "finish",
    } == TOOL_NAMES
    for spec in TOOL_SPECS:
        assert spec.parameters["additionalProperties"] is False


@pytest.mark.parametrize(
    ("name", "args", "message"),
    [
        ("read_file", {"path": "a.py", "end": "1p;id;#"}, "args.end: expected integer"),
        ("read_file", {"path": "a.py", "start": True}, "args.start: expected integer"),
        ("read_file", {"path": "a.py", "start": 5, "end": 2}, "end must not be before start"),
        ("read_file", {"path": 5}, "args.path: expected string"),
        ("write_file", {"path": "a.py", "content": 5}, "args.content: expected string"),
        ("bash", {"command": 5}, "args.command: expected string"),
        ("bash", {"command": "ls", "timeout": "soon"}, "args.timeout: expected integer"),
        ("bash", {"command": "ls", "timeout": -3}, "timeout must be positive"),
    ],
)
def test_arguments_are_validated_against_the_schemas(
    name: str, args: dict[str, object], message: str
) -> None:
    sandbox = FakeSandbox()
    result = Toolbox(sandbox).call(name, args)
    assert result.ok is False
    assert message in result.output
    assert sandbox.commands == []  # nothing reached the sandbox


def test_long_timeouts_are_capped() -> None:
    sandbox = FakeSandbox()
    Toolbox(sandbox).call("bash", {"command": "ls", "timeout": 99_999})
    assert sandbox.commands[0][1] == MAX_COMMAND_TIMEOUT_S
