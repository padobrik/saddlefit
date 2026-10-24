import base64
import re
import shlex
from dataclasses import dataclass, field

import pytest

from saddlefit.sandbox.container import ExecResult, SandboxError
from saddlefit.tools.builtin import (
    MAX_COMMAND_TIMEOUT_S,
    TOOL_NAMES,
    TOOL_SPECS,
    Toolbox,
    ToolError,
    confine,
    specs_for,
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
    out = truncate("a" * 500 + "b" * 500, 200)
    assert out.startswith("a" * 70)
    assert out.endswith("b" * 70)
    match = re.search(r"\[\.\.\. (\d+) characters cut", out)
    assert match is not None
    kept = (len(out) - len(out.lstrip("a"))) + (len(out) - len(out.rstrip("b")))
    assert int(match[1]) == 1000 - kept
    assert len(out) <= 200


@pytest.mark.parametrize("max_chars", [0, 1, 10, 40, 60, 100, 999, 1000])
def test_truncate_never_exceeds_the_cap(max_chars: int) -> None:
    assert len(truncate("x" * 1000 + "y", max_chars)) <= max_chars


def test_finish_summary_is_capped() -> None:
    box = Toolbox(FakeSandbox(), max_chars=100)
    result = box.finish("s" * 5000)
    assert result.finished
    assert len(result.output) <= 100


def test_commands_quote_paths_and_patterns() -> None:
    box = Toolbox(FakeSandbox())
    box.read_file("my file.py")
    box.read_file("a.py", start=3, end=7)
    box.list_dir()
    box.search("x; rm -rf /", "src")
    commands = [c for c, _ in box.sandbox.commands]  # type: ignore[attr-defined]
    assert commands[0] == "cat -n -- '/work/my file.py'"
    assert commands[1] == "set -o pipefail; cat -n -- /work/a.py | sed -n '3,7p'"
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


def test_large_content_is_staged_in_bounded_chunks() -> None:
    sandbox = FakeSandbox()
    Toolbox(sandbox).write_file("big.txt", "x" * 200_000)
    commands = [c for c, _ in sandbox.commands]
    staged = [c for c in commands if c.startswith("printf %s ")]
    assert len(staged) >= 5
    assert all(len(c) < 70_000 for c in commands)
    helper = next(c for c in commands if c.startswith("python3 -c"))
    assert " write @/tmp/saddlefit-" in helper
    assert commands[-1].startswith("rm -f /tmp/saddlefit-")


class BrokenSandbox:
    def __init__(self, fail_after: int = 0) -> None:
        self.calls: list[str] = []
        self.fail_after = fail_after

    def exec(self, command: str, timeout_s: float) -> ExecResult:
        self.calls.append(command)
        if command.startswith("rm -f"):
            return ExecResult(exit_code=0, output="", timed_out=False, duration_s=0.0)
        if len(self.calls) > self.fail_after:
            raise SandboxError("container is gone")
        return ExecResult(exit_code=0, output="", timed_out=False, duration_s=0.0)


def test_sandbox_failures_become_failed_results() -> None:
    result = Toolbox(BrokenSandbox()).call("list_dir", {})
    assert (result.ok, result.output) == (False, "sandbox error: container is gone")


def test_staged_chunks_are_removed_when_staging_fails() -> None:
    sandbox = BrokenSandbox(fail_after=2)
    result = Toolbox(sandbox).write_file("big.txt", "x" * 200_000)
    assert not result.ok
    assert sandbox.calls[-1].startswith("rm -f /tmp/saddlefit-")


def test_long_sandbox_errors_are_capped() -> None:
    class Noisy:
        def exec(self, command: str, timeout_s: float) -> ExecResult:
            raise SandboxError("x" * 10_000)

    result = Toolbox(Noisy(), max_chars=200).call("list_dir", {})
    assert not result.ok
    assert len(result.output) < 300
    assert "characters cut" in result.output


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("write_file", {"path": "a.py", "content": "x\ud800"}),
        ("edit_file", {"path": "a.py", "old": "\udfff", "new": "y"}),
        ("bash", {"command": "echo \ud800"}),
        ("read_file", {"path": "\ud800.py"}),
    ],
)
def test_strings_that_are_not_utf8_come_back_as_a_failed_result(
    name: str, arguments: dict[str, object]
) -> None:
    box = Toolbox(FakeSandbox())
    result = box.call(name, arguments)
    assert not result.ok
    assert "UTF-8" in result.output
    assert box.sandbox.commands == []  # type: ignore[attr-defined]


def helper_values(command: str) -> list[str]:
    """The base64 arguments of an in-sandbox helper call, decoded."""
    return [base64.b64decode(arg).decode("utf-8") for arg in shlex.split(command)[4:]]


def test_read_file_can_leave_out_line_numbers() -> None:
    box = FakeSandbox()
    Toolbox(box, line_numbers=False).call("read_file", {"path": "a.py", "start": 2, "end": 3})
    Toolbox(box).call("read_file", {"path": "a.py"})
    assert box.commands[0][0] == "set -o pipefail; cat -- /work/a.py | sed -n '2,3p'"
    assert box.commands[1][0] == "cat -n -- /work/a.py"


def test_edit_file_can_strip_copied_line_numbers() -> None:
    box = FakeSandbox()
    old, new = "    12\tdef parse(s):\n    13\t    return s\n", "    12\tdef parse(s):\n"
    Toolbox(box, strip_line_numbers=True).call(
        "edit_file", {"path": "a.py", "old": old, "new": new}
    )
    Toolbox(box).call("edit_file", {"path": "a.py", "old": old, "new": new})
    assert helper_values(box.commands[0][0])[1:] == [
        "def parse(s):\n    return s\n",
        "def parse(s):\n",
    ]
    assert helper_values(box.commands[1][0])[1:] == [old, new]  # off: taken as written


def test_edit_file_leaves_text_alone_unless_every_line_is_numbered() -> None:
    box = FakeSandbox()
    old = "    12\tdef parse(s):\nreturn s\n"
    Toolbox(box, strip_line_numbers=True).call(
        "edit_file", {"path": "a.py", "old": old, "new": "x"}
    )
    assert helper_values(box.commands[0][0])[1] == old


def test_a_disabled_tool_is_refused_but_runs_for_the_loop_itself() -> None:
    box = FakeSandbox()
    tools = Toolbox(box, enabled=frozenset({"read_file", "finish"}))
    refused = tools.call("bash", {"command": "ls"})
    assert not refused.ok
    assert refused.output == "tool not available in this harness: bash"
    assert box.commands == []
    assert tools.call("bash", {"command": "pytest -q"}, internal=True).ok
    assert box.commands[0][0] == "pytest -q"


def test_specs_for_keeps_finish() -> None:
    names = [spec.name for spec in specs_for(frozenset({"bash"}))]
    assert names == ["bash", "finish"]
