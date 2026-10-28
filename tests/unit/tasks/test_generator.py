"""The generator call: prompts (snapshot-tested), file sampling and task checks (A11, B13)."""

import dataclasses
import os
import random
import subprocess
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
    task_title,
)
from saddlefit.tasks.mining import _git_env
from saddlefit.textdiff import file_diff

SNAPSHOTS = Path(__file__).parent / "snapshots"
PROTECTED = ("tests/**", "**/test_*.py", "**/*_test.py")
EDIT = {"path": "src/dates.py", "old": "return s\n", "new": "return s.strip()\n"}
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
        "reference_edits": [EDIT],
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
        ({"reference_edits": []}, "at least one edit"),
        ({"reference_edits": [{**EDIT, "old": "nowhere"}]}, "occurs 0 times"),
        ({"reference_edits": [{**EDIT, "path": "src/other.py"}]}, "were not shown"),
        ({"reference_edits": [{**EDIT, "new": EDIT["old"]}]}, "change nothing"),
        ({"reference_edits": [{"path": "src/dates.py", "old": "", "new": "x"}]}, "new file"),
        ({"reference_edits": [{"path": "../x.py", "old": "", "new": "x"}]}, "relative and normal"),
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
            {"reference_edits": [{**EDIT, "path": "tests/test_dates.py"}]},
            "touch protected paths",
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


def test_edits_become_a_patch_that_git_applies(tmp_path: Path) -> None:
    new_file = {"path": "src/extra.py", "old": "", "new": "X = 1"}
    result = generate(FakeClient([reply(reference_edits=[EDIT, new_file])]), request())
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "dates.py").write_bytes(b"def parse(s): return s\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=_git_env())
    subprocess.run(
        ["git", "-c", "core.autocrlf=false", "apply", "-"],  # Windows runners set autocrlf
        cwd=tmp_path,
        input=result.task.reference_patch.encode("utf-8"),  # bytes: no CRs added on Windows
        check=True,
        env=_git_env(),
    )
    assert (tmp_path / "src" / "dates.py").read_bytes() == b"def parse(s): return s.strip()\n"
    assert (tmp_path / "src" / "extra.py").read_bytes() == b"X = 1"  # no newline, kept


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


def test_a_line_separator_inside_a_line_stays_in_it() -> None:
    patch = file_diff("a.py", "x = 'a\u2028b'\n", "x = 'a\u2028c'\n")
    assert patch.count("\n-x = ") == 1
    assert "No newline" not in patch


def test_a_path_with_a_tab_is_quoted_and_applies(tmp_path: Path) -> None:
    name = "src/a\tb.py"
    patch = file_diff(name, "x = 1\n", "x = 2\n")
    assert patch.startswith('diff --git "a/src/a\\tb.py" "b/src/a\\tb.py"\n')
    if os.name == "nt":
        return  # Windows has no file names with a tab; the header is what this test is about
    (tmp_path / "src").mkdir()
    (tmp_path / name).write_bytes(b"x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=_git_env())
    subprocess.run(
        ["git", "-c", "core.autocrlf=false", "apply", "-"],
        cwd=tmp_path,
        input=patch.encode("utf-8"),
        check=True,
        env=_git_env(),
    )
    assert (tmp_path / name).read_bytes() == b"x = 2\n"


def test_titles_already_covered_reach_the_prompt() -> None:
    text = render_user(dataclasses.replace(request(), covered=("Fix `canary`", "Add shout")))
    assert "# Already covered" in text
    assert "- Fix `canary`\n- Add shout" in text


def test_a_task_title_is_its_first_line_without_heading_marks() -> None:
    assert task_title("\n# `SealedStore.canary` leaks\n\nbody") == "`SealedStore.canary` leaks"
