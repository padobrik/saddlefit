"""The generator call: prompts (snapshot-tested), file sampling and task checks (A11, B13)."""

import os
import random
from pathlib import Path
from typing import Any

import pytest

from saddlefit.providers.base import ModelReply, ToolCall, Usage
from saddlefit.providers.fake import FakeClient
from saddlefit.tasks.generator import (
    SUBMIT_TOOL,
    TASK_KINDS,
    GeneratorError,
    GeneratorRequest,
    RepoFile,
    generate,
    render_system,
    render_user,
    select_files,
)

SNAPSHOTS = Path(__file__).parent / "snapshots"
PROTECTED = ("tests/**", "**/test_*.py", "**/*_test.py")
PATCH = (
    "diff --git a/src/dates.py b/src/dates.py\n--- a/src/dates.py\n+++ b/src/dates.py\n"
    "@@ -1 +1 @@\n-def parse(s): return s\n+def parse(s): return s.strip()\n"
)


def request(**changes: Any) -> GeneratorRequest:
    fields: dict[str, Any] = {
        "kind": TASK_KINDS["bugfix"],
        "repo_summary": "src/dates.py\ntests/test_dates.py\n\nREADME: parses dates.",
        "files": [RepoFile("src/dates.py", "def parse(s): return s\n")],
        "examples": ["# Fix parse_iso\n\nIt drops the offset."],
        "protected": PROTECTED,
        "existing_paths": frozenset({"src/dates.py", "tests/test_dates.py"}),
    }
    return GeneratorRequest(**{**fields, **changes})


def check_snapshot(name: str, text: str) -> None:
    """Compare with ``snapshots/<name>``; ``SADDLEFIT_UPDATE_SNAPSHOTS=1`` rewrites it."""
    path = SNAPSHOTS / name
    if os.environ.get("SADDLEFIT_UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8")


def reply(**arguments: Any) -> ModelReply:
    task: dict[str, Any] = {
        "task_md": "# parse keeps spaces\n\nparse(' 2026-10-21 ') should strip them.",
        "hidden_tests": [{"path": "tests/test_parse_spaces.py", "content": "def test(): ..."}],
        "reference_patch": PATCH,
        "visible_check": "python -m pytest -q tests/test_dates.py",
    }
    call = ToolCall(id="c1", name=SUBMIT_TOOL.name, arguments={**task, **arguments})
    return ModelReply(tool_calls=(call,), usage=Usage(prompt_tokens=900, completion_tokens=300))


def test_system_prompt_snapshot() -> None:
    check_snapshot("generate-system.md", render_system(request()))


def test_user_prompt_snapshot() -> None:
    check_snapshot("generate-user.md", render_user(request()))


def test_a_good_answer_becomes_a_task() -> None:
    result = generate(FakeClient([reply()]), request())
    assert result.task.task_md.startswith("# parse keeps spaces")
    assert result.task.hidden_tests == {"tests/test_parse_spaces.py": "def test(): ..."}
    assert result.task.reference_patch == PATCH
    assert result.usage.prompt_tokens == 900


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"task_md": "no title"}, "starting with a '# ' title"),
        ({"hidden_tests": []}, "at least one file"),
        ({"hidden_tests": [{"path": "src/check.py", "content": "x"}]}, "no protected pattern"),
        ({"hidden_tests": [{"path": "../tests/t.py", "content": "x"}]}, "stay inside"),
        ({"hidden_tests": [{"path": "tests/test_dates.py", "content": "x"}]}, "already exists"),
        ({"reference_patch": "just prose"}, "unified diff"),
        ({"reference_patch": "diff --git a/src/dates.py b/src/dates.py\n"}, "unified diff"),
        (
            {"hidden_tests": [{"path": "tests/./test_dates.py", "content": "x"}]},
            "normal form",
        ),
        ({"task_md": "# Fix it\n\nSee test_parse_spaces.py."}, "names the hidden test"),
        (
            {
                "hidden_tests": [
                    {"path": "tests/test_x.py", "content": "assert parse(' a ') == 'a-stripped'"}
                ],
                "task_md": "# Fix it\n\nassert parse(' a ') == 'a-stripped'",
            },
            "quotes the hidden test",
        ),
        (
            {"reference_patch": PATCH.replace("src/dates.py", "tests/test_dates.py")},
            "touches protected paths",
        ),
        ({"visible_check": " "}, "shell command"),
    ],
)
def test_unusable_answers_are_refused_with_their_cost(
    arguments: dict[str, Any], message: str
) -> None:
    with pytest.raises(GeneratorError, match=message) as exc:
        generate(FakeClient([reply(**arguments)]), request())
    assert exc.value.usage is not None
    assert exc.value.usage.prompt_tokens == 900


def test_an_answer_without_the_tool_is_refused() -> None:
    with pytest.raises(GeneratorError, match="exactly one submit_task"):
        generate(FakeClient([ModelReply(text="here is a task")]), request())


def test_file_sampling_fits_the_budget_and_varies() -> None:
    files = [RepoFile(f"src/m{i}.py", "x" * 100) for i in range(20)]
    first = select_files(files, random.Random(1), budget_chars=500)
    second = select_files(files, random.Random(2), budget_chars=500)
    assert len(first) == 5
    assert [f.path for f in first] == sorted(f.path for f in first)
    assert first != second


def test_every_task_kind_has_instructions() -> None:
    assert set(TASK_KINDS) == {"feature", "bugfix"}
    assert all(kind.name == name and kind.instructions for name, kind in TASK_KINDS.items())


def test_a_request_over_the_file_budget_is_refused() -> None:
    big = [RepoFile("src/big.py", "x" * 40_001)]
    with pytest.raises(ValueError, match="over 40000"):
        generate(FakeClient([reply()]), request(files=big))
