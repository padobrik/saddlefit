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


def log(store: HarnessStore, ref: str = "main") -> list[str]:
    out = subprocess.run(
        ["git", "log", "--format=%s", ref],
        cwd=store.path,
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
            ["git", "branch", "--list"], cwd=store.path, capture_output=True, text=True, check=True
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
