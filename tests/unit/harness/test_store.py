import difflib
import json
import os
import subprocess
from pathlib import Path

import pytest

from saddlefit.harness.loop_config import default_loop_config, load_loop_config
from saddlefit.harness.store import HarnessStore, HarnessStoreError


def patch_for(path: str, old: str, new: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{old}\n+{new}\n"
    )


NEW_SKILL = (
    "diff --git a/skills/pydantic/SKILL.md b/skills/pydantic/SKILL.md\n"
    "new file mode 100644\n--- /dev/null\n+++ b/skills/pydantic/SKILL.md\n"
    "@@ -0,0 +1 @@\n+- `.dict()` -> `.model_dump()`\n"
)
LESSON = patch_for("EXPERIENCE.md", "# Lessons", "# Lessons (fitted)")


@pytest.fixture
def store(tmp_path: Path) -> HarnessStore:
    store = HarnessStore(tmp_path / "harness")
    store.init()
    return store


def git_env(home: Path) -> dict[str, str]:
    """A clean environment: no GIT_DIR or GIT_INDEX_FILE from a hook, no user config."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }


def log(store: HarnessStore, ref: str = "main") -> list[str]:
    out = subprocess.run(
        ["git", "log", "--format=%s", ref],
        cwd=store.path,
        env=git_env(store.path),
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.splitlines()


def test_init_writes_defaults_and_commits(store: HarnessStore) -> None:
    assert len(store.head()) == 40
    assert load_loop_config(store.path / "loop.yaml") == default_loop_config()
    for name in ("systemprompt.md", "tool_guidance.md", "EXPERIENCE.md"):
        assert (store.path / name).is_file()
    with pytest.raises(HarnessStoreError, match="already exists"):
        store.init()


def test_candidate_then_accept(store: HarnessStore) -> None:
    before = store.head()
    candidate = store.propose("01JB2Z0C7H", NEW_SKILL, "skill: pydantic mapping table")
    assert store.head() == before  # main is untouched until acceptance
    assert not (store.path / "skills/pydantic/SKILL.md").exists()
    after = store.accept("01JB2Z0C7H")
    assert after != before
    assert (store.path / "skills/pydantic/SKILL.md").read_text().startswith("- `.dict()`")
    assert store.head(f"{after}^2") == candidate


def test_reject_leaves_main_alone(store: HarnessStore) -> None:
    before = store.head()
    store.propose("p1", LESSON, "lesson")
    store.reject("p1")
    assert store.head() == before
    with pytest.raises(HarnessStoreError):
        store.head("candidate/p1")


@pytest.mark.parametrize(
    "patch",
    [
        patch_for("saddlefit.yaml", "project: p", "project: q"),
        patch_for("../saddlefit.yaml", "a", "b"),
        patch_for("tasks/sealed/x/check.sh", "a", "b"),
    ],
    ids=["config", "traversal", "sealed"],
)
def test_patches_outside_the_allowlist_are_refused(store: HarnessStore, patch: str) -> None:
    before = store.head()
    with pytest.raises(HarnessStoreError, match="outside the allowlist"):
        store.propose("p2", patch, "sneaky")
    assert store.head() == before


def test_patch_that_does_not_apply_is_refused_and_cleaned_up(store: HarnessStore) -> None:
    with pytest.raises(HarnessStoreError, match="apply"):
        store.propose("p3", patch_for("EXPERIENCE.md", "no such line", "x"), "bad")
    with pytest.raises(HarnessStoreError):
        store.head("candidate/p3")
    with pytest.raises(HarnessStoreError, match="no files"):
        store.propose("p4", "not a diff", "empty")
    with pytest.raises(HarnessStoreError, match="invalid proposal id"):
        store.propose("../x", LESSON, "bad id")


def test_prune_reverts_with_reason(store: HarnessStore) -> None:
    store.propose("p5", LESSON, "lesson")
    accepted = store.accept("p5")
    store.prune(accepted, "removal gave wins - losses = 1 >= 0")
    assert (store.path / "EXPERIENCE.md").read_text() == "# Lessons\n"
    assert log(store)[0] == f"prune {accepted[:12]}"


def test_tag_and_export(store: HarnessStore, tmp_path: Path) -> None:
    store.propose("p6", LESSON, "lesson")
    store.accept("p6")
    store.tag("fit-2026-10-21-1")
    out = tmp_path / "exported"
    store.export("fit-2026-10-21-1", out)
    assert (out / "EXPERIENCE.md").read_text() == "# Lessons (fitted)\n"
    assert not (out / ".git").exists()


@pytest.mark.parametrize("bad", ["-x", "--upload-pack=evil", "a b", "x\n", ""])
def test_ids_names_and_commits_that_could_be_git_options_are_refused(
    store: HarnessStore, bad: str
) -> None:
    with pytest.raises(HarnessStoreError, match="invalid"):
        store.propose(bad, LESSON, "x")
    with pytest.raises(HarnessStoreError, match="invalid"):
        store.accept(bad)
    with pytest.raises(HarnessStoreError, match="invalid"):
        store.reject(bad)
    with pytest.raises(HarnessStoreError, match="invalid"):
        store.tag(bad)
    with pytest.raises(HarnessStoreError, match="invalid"):
        store.prune(bad, "reason")


def test_symlinks_are_refused(store: HarnessStore) -> None:
    link = (
        "diff --git a/skills/leak b/skills/leak\nnew file mode 120000\n"
        "--- /dev/null\n+++ b/skills/leak\n@@ -0,0 +1 @@\n+/etc/passwd\n"
        "\\ No newline at end of file\n"
    )
    with pytest.raises(HarnessStoreError, match="symlink"):
        store.propose("p-link", link, "link")
    assert (
        "candidate/p-link"
        not in subprocess.run(
            ["git", "branch", "--list"],
            cwd=store.path,
            env=git_env(store.path),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )


def test_export_ignores_export_ignore_attributes(store: HarnessStore, tmp_path: Path) -> None:
    hide = (
        "diff --git a/skills/.gitattributes b/skills/.gitattributes\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/skills/.gitattributes\n@@ -0,0 +1 @@\n+* export-ignore\n"
    )
    store.propose("p-hide", hide + NEW_SKILL, "hide")
    store.accept("p-hide")
    dest = tmp_path / "out"
    store.export("main", dest)
    assert (dest / "skills" / "pydantic" / "SKILL.md").is_file()
    assert (dest / "skills" / ".gitattributes").is_file()


def test_patches_reach_git_as_bytes_so_windows_keeps_lf(
    store: HarnessStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs: list[object] = []
    real_run = subprocess.run

    def recording_run(*args: object, **kwargs: object) -> object:
        if kwargs.get("input") is not None:
            inputs.append(kwargs["input"])
            assert not kwargs.get("text"), "text mode turns \\n into \\r\\n on Windows"
        return real_run(*args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(subprocess, "run", recording_run)
    store.propose("p-bytes", LESSON, "lesson")
    assert inputs
    assert all(isinstance(i, bytes) and b"\r\n" not in i for i in inputs)


def test_a_failing_cleanup_does_not_hide_the_original_error(
    store: HarnessStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_git = HarnessStore._git

    def flaky_git(self: HarnessStore, *args: str, **kwargs: object) -> str:
        if args[0] in {"commit", "branch"}:
            raise HarnessStoreError(f"git {args[0]} failed: boom")
        return real_git(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(HarnessStore, "_git", flaky_git)
    with pytest.raises(HarnessStoreError, match="git commit failed"):
        store.propose("p-flaky", LESSON, "lesson")


def test_a_conflicting_prune_is_aborted_cleanly(store: HarnessStore) -> None:
    store.propose("p-1", LESSON, "first")
    first = store.accept("p-1")
    store.propose("p-2", patch_for("EXPERIENCE.md", "# Lessons (fitted)", "# Lessons (v2)"), "2")
    store.accept("p-2")
    with pytest.raises(HarnessStoreError, match="revert"):
        store.prune(first, "conflicts with p-2")
    assert not (store.path / ".git" / "REVERT_HEAD").exists()
    store.propose("p-3", NEW_SKILL, "still usable")


def test_export_to_a_relative_directory(
    store: HarnessStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    store.export("main", Path("out"))
    assert (tmp_path / "out" / "EXPERIENCE.md").is_file()
    assert not (store.path / "out").exists()


def commit_raw(store: HarnessStore, rel: str, content: str | None) -> None:
    """Change the store behind its back, as a corrupted or foreign store would look."""
    path = store.path / rel
    if content is None:
        path.unlink()
    else:
        path.write_text(content, encoding="utf-8")
    env = git_env(store.path)
    for args in (
        ["add", "--all"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "raw"],
    ):
        subprocess.run(["git", *args], cwd=store.path, env=env, check=True, capture_output=True)


def test_init_records_the_store_schema_version(store: HarnessStore) -> None:
    info = json.loads((store.path / "harness.json").read_text(encoding="utf-8"))
    assert info == {"schema_version": 1}


def test_proposals_cannot_touch_the_version_marker(store: HarnessStore) -> None:
    with pytest.raises(HarnessStoreError, match="outside the allowlist"):
        store.propose("p-ver", patch_for("harness.json", '{"schema_version":1}', "{}"), "x")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "missing or non-integer schema_version"),
        ('{"schema_version": "1"}', "missing or non-integer schema_version"),
        ('{"schema_version": true}', "missing or non-integer schema_version"),
        ('{"schema_version": 2}', "schema_version 2 is not supported"),
        ("not json", "unreadable harness.json"),
    ],
)
def test_a_store_with_a_bad_version_is_refused(
    store: HarnessStore, tmp_path: Path, content: str | None, message: str
) -> None:
    commit_raw(store, "harness.json", content)
    with pytest.raises(HarnessStoreError, match=message):
        store.propose("p-after", LESSON, "x")
    with pytest.raises(HarnessStoreError, match=message):
        store.export("main", tmp_path / "out")


def unified(path: str, old: str, new: str) -> str:
    """A git-style patch turning ``old`` into ``new`` at ``path``."""
    body = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}" if old else "/dev/null",
            tofile=f"b/{path}",
        )
    )
    header = f"diff --git a/{path} b/{path}\n" + ("new file mode 100644\n" if not old else "")
    return header + body


@pytest.mark.parametrize("mode_line", ["new file mode  120000", "new file mode 120000\r"])
def test_a_symlink_hidden_by_odd_spacing_is_refused(store: HarnessStore, mode_line: str) -> None:
    link = (
        f"diff --git a/skills/leak b/skills/leak\n{mode_line}\n"
        "--- /dev/null\n+++ b/skills/leak\n@@ -0,0 +1 @@\n+/etc/passwd\n"
        "\\ No newline at end of file\n"
    )
    with pytest.raises(HarnessStoreError):
        store.propose("p-odd", link, "link")
    assert (
        "candidate/p-odd"
        not in subprocess.run(
            ["git", "branch", "--list"],
            cwd=store.path,
            env=git_env(store.path),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )


def test_a_proposal_may_change_at_most_200_lines(store: HarnessStore) -> None:
    ok = unified("skills/big/SKILL.md", "", "".join(f"line {i}\n" for i in range(200)))
    store.propose("p-200", ok, "200 lines")
    too_big = unified("skills/bigger/SKILL.md", "", "".join(f"line {i}\n" for i in range(201)))
    with pytest.raises(HarnessStoreError, match="200"):
        store.propose("p-201", too_big, "201 lines")


def test_an_invalid_loop_yaml_is_refused(store: HarnessStore) -> None:
    current = (store.path / "loop.yaml").read_text(encoding="utf-8")
    broken = current.replace("max_steps:", "max_steps: banana\nignored:", 1)
    with pytest.raises(HarnessStoreError, match=r"loop\.yaml"):
        store.propose("p-loop", unified("loop.yaml", current, broken), "break loop.yaml")
    assert (store.path / "loop.yaml").read_text(encoding="utf-8") == current


def test_a_conflicting_accept_is_aborted_cleanly(store: HarnessStore) -> None:
    store.propose("p-a", LESSON, "a")
    store.propose("p-b", patch_for("EXPERIENCE.md", "# Lessons", "# Lessons (other)"), "b")
    store.accept("p-a")
    with pytest.raises(HarnessStoreError, match="merge"):
        store.accept("p-b")
    assert not (store.path / ".git" / "MERGE_HEAD").exists()
    store.propose("p-c", NEW_SKILL, "still usable")


def test_export_writes_the_committed_bytes(store: HarnessStore, tmp_path: Path) -> None:
    crlf = unified("skills/.gitattributes", "", "* text eol=crlf\n")
    store.propose("p-eol", crlf + NEW_SKILL, "eol")
    store.accept("p-eol")
    dest = tmp_path / "out"
    store.export("main", dest)
    assert b"\r" not in (dest / "skills" / "pydantic" / "SKILL.md").read_bytes()


def test_a_hidden_deletion_of_the_version_marker_is_refused(store: HarnessStore) -> None:
    marker = (store.path / "harness.json").read_text(encoding="utf-8")
    # git may pair the deletion with the new file as a rename and report only the new path;
    # the deletion comes first, with no diff --git header, so the text check cannot see it
    # and --recount cannot fold it into another file's hunk
    patch = ("--- a/harness.json\n+++ /dev/null\n@@ -1 +0,0 @@\n" + f"-{marker}") + unified(
        "skills/moved.json", "", marker
    )
    with pytest.raises(HarnessStoreError, match="allowlist"):
        store.propose("p-hide-marker", patch, "move the marker")
    assert (store.path / "harness.json").read_text(encoding="utf-8") == marker


def test_a_hunk_with_wrong_line_counts_still_applies(store: HarnessStore) -> None:
    # as a tutor wrote it: four old lines under a header that says three
    patch = (
        "diff --git a/loop.yaml b/loop.yaml\n--- a/loop.yaml\n+++ b/loop.yaml\n"
        "@@ -9,3 +9,3 @@\n   reread_task: true\n   run: pytest -q\n-  max_retries: 1\n"
        "+  max_retries: 3\n loop_detection:\n"
    )
    store.propose("p9", patch, "more retries")
    store.accept("p9")
    assert load_loop_config(store.path / "loop.yaml").finish_gate.max_retries == 3


def test_a_hunk_whose_context_does_not_match_is_refused(store: HarnessStore) -> None:
    patch = (
        "diff --git a/loop.yaml b/loop.yaml\n--- a/loop.yaml\n+++ b/loop.yaml\n"
        "@@ -9,3 +9,3 @@\n   reread_task: false\n-  max_retries: 1\n+  max_retries: 3\n"
    )
    with pytest.raises(HarnessStoreError, match="git apply failed"):
        store.propose("p9", patch, "more retries")
