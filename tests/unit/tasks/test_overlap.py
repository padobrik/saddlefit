"""Which functions a reference patch changes: what tells a repeated task from a new one."""

import difflib

from saddlefit.tasks.overlap import changed_symbols

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
    assert symbols("b\n", old="a\n", path="docs/notes.md") == {"docs/notes.md"}


def test_a_patch_that_does_not_apply_counts_its_files_as_a_whole() -> None:
    stale = patch("src/store.py", "something else\n", "x\n")
    assert changed_symbols(stale, {"src/store.py": OLD}.get) == {"src/store.py"}


def test_no_patch_changes_nothing() -> None:
    assert changed_symbols("", {}.get) == frozenset()
