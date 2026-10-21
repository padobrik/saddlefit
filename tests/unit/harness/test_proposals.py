"""Proposal validation; includes required test 1 (paths outside the allowlist are rejected)."""

from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError

from saddlefit.harness.proposals import (
    Proposal,
    ProposalError,
    apply_proposal,
    scripts_to_lint,
)
from saddlefit.harness.store import HarnessStore, HarnessStoreError
from saddlefit.schema_registry import SCHEMA_MODELS

PROPOSAL_ID = "01JB2Z0C7H2P6S8E4F1A9B3D5G"


def edit(path: str, old: str, new: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{old}\n+{new}\n"
    )


def new_file(path: str, line: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1 @@\n+{line}\n"
    )


def proposal(patch: str, **changes: object) -> Proposal:
    fields: dict[str, object] = {
        "schema_version": 1,
        "proposal_id": PROPOSAL_ID,
        "source": "tutor",
        "level": "tools",
        "failure_tags": ["gave_up"],
        "evidence_run_ids": ["01JB2Y8Q4V9N1K3D7M5R6T0W2X"],
        "technique": "finish gate: run the visible tests",
        "rationale": "In 3 of 4 failed runs the student finished without running the tests.",
        "patch": patch,
        "expected_effect": "Fewer gave_up failures",
        "context_tokens_delta": 40,
    }
    return Proposal.model_validate({**fields, **changes})


class RecordingLinter:
    def __init__(self, problems: list[str] | None = None) -> None:
        self.problems = problems or []
        self.calls: list[dict[str, bytes]] = []

    def lint(self, scripts: Mapping[str, bytes]) -> list[str]:
        self.calls.append(dict(scripts))
        return self.problems


@pytest.fixture
def store(tmp_path: Path) -> HarnessStore:
    store = HarnessStore(tmp_path / "harness")
    store.init()
    return store


def has_candidate(store: HarnessStore) -> bool:
    try:
        store.head(f"candidate/{PROPOSAL_ID}")
    except HarnessStoreError:
        return False
    return True


@pytest.mark.parametrize(
    "path",
    ["saddlefit.yaml", "../saddlefit.yaml", "tasks/sealed/x/check.sh", "harness.json", "README.md"],
)
def test_a_proposal_outside_the_allowlist_is_rejected(store: HarnessStore, path: str) -> None:
    """Required test 1."""
    before = store.head()
    linter = RecordingLinter()
    with pytest.raises(ProposalError, match="outside the allowlist"):
        apply_proposal(store, proposal(new_file(path, "x")), linter)
    assert store.head() == before
    assert not has_candidate(store)
    assert linter.calls == []


def test_a_valid_proposal_becomes_a_candidate(store: HarnessStore) -> None:
    before = store.head()
    linter = RecordingLinter()
    sha = apply_proposal(store, proposal(edit("EXPERIENCE.md", "# Lessons", "# Lessons!")), linter)
    assert store.head(f"candidate/{PROPOSAL_ID}") == sha
    assert store.head() == before  # main moves only on accept
    assert linter.calls == []  # no scripts, no linting


def test_new_and_changed_scripts_are_linted(store: HarnessStore) -> None:
    patch = new_file("tools/find_old.py", "print(1)") + new_file("skills/a/run.sh", "echo hi")
    linter = RecordingLinter()
    apply_proposal(store, proposal(patch), linter)
    assert linter.calls == [{"tools/find_old.py": b"print(1)\n", "skills/a/run.sh": b"echo hi\n"}]


def test_a_script_failing_the_linter_is_rejected(store: HarnessStore) -> None:
    before = store.head()
    linter = RecordingLinter(["tools/x.py:1:1: F821 Undefined name `y`"])
    with pytest.raises(ProposalError, match="F821"):
        apply_proposal(store, proposal(new_file("tools/x.py", "y")), linter)
    assert store.head() == before
    assert not has_candidate(store)


def test_tools_hold_only_scripts(store: HarnessStore) -> None:
    linter = RecordingLinter()
    with pytest.raises(ProposalError, match=r"only \.py and \.sh"):
        apply_proposal(store, proposal(new_file("tools/run", "#!/bin/sh")), linter)
    assert not has_candidate(store)
    assert linter.calls == []


def test_store_refusals_become_proposal_errors(store: HarnessStore) -> None:
    patch = edit("EXPERIENCE.md", "not the first line", "x")
    with pytest.raises(ProposalError, match="git apply failed"):
        apply_proposal(store, proposal(patch), RecordingLinter())
    assert not has_candidate(store)


def test_scripts_to_lint() -> None:
    files = {"skills/a/SKILL.md": b"x", "tools/a.py": b"a", "skills/b/x.sh": b"b"}
    assert scripts_to_lint(files) == {"tools/a.py": b"a", "skills/b/x.sh": b"b"}


@pytest.mark.parametrize(
    "changes",
    [
        {"proposal_id": "not-a-ulid"},
        {"proposal_id": "--upload-pack=x"},
        {"evidence_run_ids": ["../x"]},
        {"evidence_run_ids": ["01JB2Y8Q4V9N1K3D7M5R6T0W2"]},
        {"source": "student"},
        {"level": "budget"},
        {"failure_tags": ["unknown_tag"]},
        {"schema_version": 2},
        {"patch": ""},
        {"extra": 1},
    ],
)
def test_malformed_proposals_are_refused(changes: dict[str, object]) -> None:
    changes = dict(changes)
    with pytest.raises(ValidationError):
        proposal(str(changes.pop("patch", "x")), **changes)


def test_the_proposal_schema_is_generated() -> None:
    assert SCHEMA_MODELS["proposal"] is Proposal


SECOND_ID = "01JB2Z0C7H2P6S8E4F1A9B3D5H"
DELETE_SCRIPT = (
    "diff --git a/tools/x.py b/tools/x.py\ndeleted file mode 100644\n--- a/tools/x.py\n"
    "+++ /dev/null\n@@ -1 +0,0 @@\n-print(1)\n"
)


def accepted_script(store: HarnessStore) -> None:
    apply_proposal(store, proposal(new_file("tools/x.py", "print(1)")), RecordingLinter())
    store.accept(PROPOSAL_ID)


def test_a_modified_script_is_linted_with_its_new_bytes(store: HarnessStore) -> None:
    accepted_script(store)
    linter = RecordingLinter()
    patch = edit("tools/x.py", "print(1)", "print(2)")
    apply_proposal(store, proposal(patch, proposal_id=SECOND_ID), linter)
    assert linter.calls == [{"tools/x.py": b"print(2)\n"}]


def test_a_deleted_script_needs_no_linting(store: HarnessStore) -> None:
    accepted_script(store)
    linter = RecordingLinter()
    sha = apply_proposal(store, proposal(DELETE_SCRIPT, proposal_id=SECOND_ID), linter)
    assert store.head(f"candidate/{SECOND_ID}") == sha
    assert linter.calls == []
