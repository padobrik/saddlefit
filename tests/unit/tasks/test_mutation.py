"""Injected bugs: mutation sites, related tests, the reference patch and the commit (A11)."""

import random
import subprocess
from pathlib import Path

import pytest

from saddlefit.providers.base import ModelReply, ProviderError, ToolCall, Usage
from saddlefit.providers.fake import FakeClient
from saddlefit.tasks.mining import _git_env
from saddlefit.tasks.mutation import (
    ISSUE_TOOL,
    MUTATION_REF,
    Site,
    apply,
    commit_mutation,
    drop_mutation,
    pick,
    reference_patch,
    related_tests,
    sites,
    write_issue,
)

SOURCE = (
    "def is_leap(year: int) -> bool:\n"
    "    # every fourth year, not 100, again 400\n"
    "    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)\n"
)


def test_sites_cover_operators_names_and_integers() -> None:
    found = {(s.old, s.new) for s in sites(SOURCE)}
    assert ("==", "!=") in found
    assert ("and", "or") in found
    assert ("4", "5") in found
    assert sites("def broken(:\n") == []


def test_a_mutation_changes_one_token_and_keeps_the_rest() -> None:
    mutated = apply(SOURCE, Site(3, 20, "==", "!="))
    assert mutated == SOURCE.replace("% 4 == 0", "% 4 != 0", 1)
    with pytest.raises(ValueError, match="no '<'"):
        apply(SOURCE, Site(3, 20, "<", "<="))


def test_pick_returns_a_compiling_mutation() -> None:
    mutation = pick("src/toykit/dates.py", SOURCE, random.Random(3))
    assert mutation is not None
    assert mutation.mutated != SOURCE
    compile(mutation.mutated, "x.py", "exec")


def test_related_tests_import_the_module() -> None:
    tests = {
        "tests/test_dates.py": "from toykit.dates import is_leap\n",
        "tests/test_text.py": "from toykit import text\n",
        "tests/test_other.py": "import json\n",
    }
    assert related_tests("src/toykit/dates.py", tests) == ["tests/test_dates.py"]
    assert related_tests("src/toykit/text.py", tests) == ["tests/test_text.py"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=_git_env()
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "dates.py").write_text(SOURCE, encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "src/dates.py")
    identity = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.hooksPath=/dev/null"]
    git(root, *identity, "commit", "-qm", "init")
    return root


def test_the_mutated_tree_is_a_commit_under_a_saddlefit_ref(repo: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    mutation = pick("src/dates.py", SOURCE, random.Random(1))
    assert mutation is not None
    sha = commit_mutation(repo, head, mutation, "mut-bugfix-x-0123456789ab")
    assert git(repo, "rev-parse", f"{MUTATION_REF}/mut-bugfix-x-0123456789ab") == sha
    assert git(repo, "show", f"{sha}:src/dates.py") + "\n" == mutation.mutated
    # nothing the user works with moved
    assert git(repo, "rev-parse", "HEAD") == head
    assert (repo / "src" / "dates.py").read_text(encoding="utf-8") == SOURCE
    assert git(repo, "status", "--porcelain") == ""
    drop_mutation(repo, "mut-bugfix-x-0123456789ab")
    assert git(repo, "for-each-ref", MUTATION_REF) == ""


def test_the_reference_patch_undoes_the_mutation(repo: Path) -> None:
    mutation = pick("src/dates.py", SOURCE, random.Random(1))
    assert mutation is not None
    (repo / "src" / "dates.py").write_text(mutation.mutated, encoding="utf-8")
    patch = reference_patch(mutation)
    assert patch.startswith("diff --git a/src/dates.py b/src/dates.py\n")
    subprocess.run(
        ["git", "apply", "-"], cwd=repo, input=patch, text=True, check=True, env=_git_env()
    )
    assert (repo / "src" / "dates.py").read_text(encoding="utf-8") == SOURCE


def issue(task_md: str) -> ModelReply:
    call = ToolCall(id="i", name=ISSUE_TOOL, arguments={"task_md": task_md})
    return ModelReply(tool_calls=(call,), usage=Usage(prompt_tokens=500, completion_tokens=80))


def test_an_issue_becomes_a_task_with_the_hidden_tests() -> None:
    mutation = pick("src/dates.py", SOURCE, random.Random(1))
    assert mutation is not None
    hidden = {"tests/test_dates.py": "def test(): ..."}
    result = write_issue(
        FakeClient([issue("# Leap years are wrong\n\n`is_leap(2000)` says no.")]),
        mutation,
        ["tests/test_dates.py"],
        hidden,
        visible_check="pytest -q tests/test_dates.py",
    )
    assert result.task.hidden_tests == hidden
    assert result.task.visible_check == "pytest -q tests/test_dates.py"
    assert result.task.reference_patch == reference_patch(mutation)


def test_an_issue_that_quotes_the_fixed_line_is_refused() -> None:
    mutation = pick("src/dates.py", SOURCE, random.Random(1))
    assert mutation is not None
    line = SOURCE.splitlines()[mutation.site.line - 1].strip()
    with pytest.raises(ProviderError, match="quotes the original line") as exc:
        write_issue(
            FakeClient([issue(f"# Bug\n\nIt should be `{line}`.")]),
            mutation,
            ["tests/test_dates.py"],
            {},
            visible_check="true",
        )
    assert exc.value.usage is not None


def test_line_numbers_follow_tokenize_across_a_form_feed() -> None:
    source = "x = 1\n\x0c\ndef f(a):\n    return a < 2\n"
    mutation = pick("m.py", source, random.Random(0))
    assert mutation is not None
    assert sum(a != b for a, b in zip(mutation.original, mutation.mutated, strict=False)) >= 1
    for site in sites(source):
        apply(source, site)  # every site tokenize reports is where apply looks


def test_the_reference_patch_ignores_the_users_diff_config(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[diff]\n\tnoprefix = true\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    mutation = pick("src/dates.py", SOURCE, random.Random(1))
    assert mutation is not None
    patch = reference_patch(mutation)
    assert "\n--- a/src/dates.py\n+++ b/src/dates.py\n" in patch
    (repo / "src" / "dates.py").write_text(mutation.mutated, encoding="utf-8")
    subprocess.run(
        ["git", "apply", "-"], cwd=repo, input=patch, text=True, check=True, env=_git_env()
    )
    assert (repo / "src" / "dates.py").read_text(encoding="utf-8") == SOURCE
