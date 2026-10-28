"""Which functions a reference patch changes: what tells a repeated task from a new one."""

import difflib
import os
import re
from pathlib import Path

import pytest

from saddlefit.errors import UserError
from saddlefit.loops.generate import NoCandidate
from saddlefit.project import _refuse_a_repeat
from saddlefit.tasks.mutation import Mutation, Site
from saddlefit.tasks.overlap import (
    RepeatGuard,
    changed_symbols,
    load_repeat_index,
    save_repeat_index,
)

OLD = """import os


LIMIT = 3


class Store:
    def canary(self, task_id):
        return open(task_id).read()

    def paths(self, task_id):
        return [task_id]


def helper(x):
    return x + 1
"""


def patch(path: str, old: str, new: str) -> str:
    body = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}"
        )
    )
    return f"diff --git a/{path} b/{path}\n{body}"


def symbols(new: str, old: str = OLD, path: str = "src/store.py") -> frozenset[str]:
    return changed_symbols(patch(path, old, new), {path: old}.get)


def test_a_method_change_names_the_method_not_the_class() -> None:
    new = OLD.replace("return open(task_id).read()", "return open(task_id).read().strip()")
    assert symbols(new) == {"src/store.py::Store.canary"}


def test_two_fixes_in_different_methods_do_not_meet() -> None:
    canary = symbols(OLD.replace("open(task_id).read()", "open(task_id).read().strip()"))
    paths = symbols(OLD.replace("return [task_id]", "return sorted([task_id])"))
    assert not canary & paths


def test_a_new_function_between_two_others_names_only_itself() -> None:
    new = OLD.replace("def helper(x):", "def added(y):\n    return y\n\n\ndef helper(x):")
    assert symbols(new) == {"src/store.py::added"}


def test_an_added_import_does_not_count() -> None:
    assert symbols(OLD.replace("import os\n", "import os\nimport errno\n")) == frozenset()


def test_other_top_level_code_counts_as_the_file() -> None:
    assert symbols(OLD.replace("LIMIT = 3", "LIMIT = 4")) == {"src/store.py"}


def test_a_file_that_is_not_python_counts_as_a_whole() -> None:
    assert symbols("b\n", old="a\n", path="docs/notes.md") == {"docs/notes.md::*"}


def test_a_patch_that_does_not_apply_counts_its_files_as_a_whole() -> None:
    stale = patch("src/store.py", "something else\n", "x\n")
    assert changed_symbols(stale, {"src/store.py": OLD}.get) == {"src/store.py::*"}


def test_no_patch_changes_nothing() -> None:
    assert changed_symbols("", {}.get) == frozenset()


def test_a_form_feed_inside_a_string_does_not_shift_the_lines() -> None:
    old = 'def first():\n    return "a\x0cb"\n\n\ndef second():\n    return 1\n'
    new = old.replace("return 1", "return 2")
    assert symbols(new, old=old) == {"src/store.py::second"}


def test_a_user_git_config_that_converts_line_endings_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a home whose git config converts line endings, as Git for Windows ships it
    (tmp_path / ".gitconfig").write_text("[core]\n\tautocrlf = true\n", encoding="utf-8")
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(tmp_path))
    new = OLD.replace("return [task_id]", "return sorted([task_id])")
    assert symbols(new) == {"src/store.py::Store.paths"}


def test_a_mutation_in_a_covered_function_is_refused_before_the_generator_call() -> None:
    mutation = Mutation(
        path="src/store.py",
        original=OLD,
        mutated=OLD.replace("x + 1", "x - 1"),
        site=Site(line=16, col=13, old="+", new="-"),
    )
    covered = RepeatGuard({"mut-bugfix-old": frozenset({"src/store.py::helper"})})
    with pytest.raises(
        NoCandidate, match=re.escape("repeats mut-bugfix-old: both change src/store.py::helper")
    ):
        _refuse_a_repeat(covered, mutation)
    _refuse_a_repeat(RepeatGuard({"other": frozenset({"src/store.py::Store.paths"})}), mutation)


@pytest.mark.skipif(os.name == "nt", reason="Windows has no quote in file names")
def test_a_quoted_file_name_is_still_read() -> None:
    path = 'src/we"ird.py'
    quoted = '"a/src/we\\"ird.py"', '"b/src/we\\"ird.py"'  # how git writes such a name
    body = "".join(
        difflib.unified_diff(
            OLD.splitlines(keepends=True),
            OLD.replace("x + 1", "x + 2").splitlines(keepends=True),
            *quoted,
        )
    )
    patch_text = f"diff --git {quoted[0]} {quoted[1]}\n{body}"
    assert changed_symbols(patch_text, {path: OLD}.get) == {f"{path}::helper"}


def test_the_repeat_index_round_trips_and_starts_empty(tmp_path: Path) -> None:
    path = tmp_path / "repeats.json"
    assert load_repeat_index(path).tasks == {}
    guard = RepeatGuard({"gen-bugfix-a": frozenset({"src/x.py::f"})})
    guard.check(
        "gen-bugfix-b",
        patch("src/store.py", OLD, OLD.replace("x + 1", "x + 2")),
        {"src/store.py": OLD}.get,
    )
    guard.kept("gen-bugfix-b")
    save_repeat_index(path, guard.index())
    assert load_repeat_index(path).tasks == {
        "gen-bugfix-a": ["src/x.py::f"],
        "gen-bugfix-b": ["src/store.py::helper"],
    }


def test_a_new_file_names_its_functions_not_itself() -> None:
    new_file = "def shout(text):\n    return text.upper()\n"
    assert symbols(new_file, old="") == {"src/store.py::shout"}


def test_two_commands_at_once_see_and_keep_each_others_tasks(tmp_path: Path) -> None:
    path = tmp_path / "repeats.json"

    def recorded() -> dict[str, frozenset[str]]:
        return {k: frozenset(v) for k, v in load_repeat_index(path).tasks.items()}

    first, second = RepeatGuard({}, recorded=recorded), RepeatGuard({}, recorded=recorded)
    fix = patch("src/store.py", OLD, OLD.replace("x + 1", "x + 2"))
    assert first.check("gen-a", fix, {"src/store.py": OLD}.get) is None
    save_repeat_index(path, first.index(adding="gen-a"))
    first.kept("gen-a")
    # the second command started before gen-a was kept, and still catches its repeat
    assert second.check("gen-b", fix, {"src/store.py": OLD}.get) == (
        "repeats gen-a: both change src/store.py::helper"
    )
    other = patch("src/store.py", OLD, OLD.replace("return [task_id]", "return []"))
    assert second.check("gen-c", other, {"src/store.py": OLD}.get) is None
    save_repeat_index(path, second.index(adding="gen-c"))
    assert set(load_repeat_index(path).tasks) == {"gen-a", "gen-c"}  # gen-a's entry survives


def test_a_function_defined_in_a_try_block_keeps_its_name() -> None:
    old = (
        "try:\n    import fast\nexcept ImportError:\n\n    def speed():\n        return 1\n"
        "\n\nLIMIT = 3\n"
    )
    assert symbols(old.replace("return 1", "return 2"), old=old) == {"src/store.py::speed"}


def test_an_unreadable_repeat_index_is_a_user_error(tmp_path: Path) -> None:
    path = tmp_path / "repeats.json"
    for text in ("{not json", "null", "[]"):
        path.write_text(text, encoding="utf-8")
        with pytest.raises(UserError, match="titles only"):
            load_repeat_index(path)


def test_a_whole_file_meets_every_symbol_in_it_but_top_level_code_does_not() -> None:
    whole = RepeatGuard({"gen-a": frozenset({"src/store.py::*"})})
    assert whole.repeats(frozenset({"src/store.py::helper"})) == (
        "repeats gen-a: both change src/store.py::helper"
    )
    top = RepeatGuard({"gen-a": frozenset({"src/store.py"})})
    assert top.repeats(frozenset({"src/store.py::helper"})) is None
    assert (
        RepeatGuard({"gen-a": frozenset({"src/other.py::*"})}).repeats(
            frozenset({"src/store.py::helper"})
        )
        is None
    )
