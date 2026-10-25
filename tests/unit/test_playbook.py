"""The starter playbook: every entry applies to a fresh harness and reaches the tutor (A6)."""

from collections.abc import Mapping
from pathlib import Path

import pytest

from saddlefit.harness.loop_config import default_loop_config, dump_loop_config, load_loop_config
from saddlefit.harness.proposals import apply_proposal
from saddlefit.harness.store import HarnessStore
from saddlefit.playbook import applied, for_tags, proposal_for, starter_playbook, tutor_lines


class NoLint:
    def lint(self, scripts: Mapping[str, bytes]) -> list[str]:
        return []


def test_the_starter_playbook_names_the_spec_mechanics() -> None:
    ids = {e.id for e in starter_playbook()}
    # A6: a finish gate that re-reads the task, signature-level loop detection, a context budget
    assert {"finish-gate-reread", "loop-detection-tight", "context-budget-tight"} <= ids


@pytest.mark.parametrize("entry", starter_playbook(), ids=lambda e: e.id)
def test_every_entry_applies_to_a_fresh_harness(tmp_path: Path, entry) -> None:  # type: ignore[no-untyped-def]
    store = HarnessStore(tmp_path / "harness")
    store.init()
    text = (store.path / "loop.yaml").read_text(encoding="utf-8")
    proposal = proposal_for([entry], text, load_loop_config(store.path / "loop.yaml"))
    assert proposal is not None
    assert proposal.source == "playbook"
    ref = apply_proposal(store, proposal, NoLint())
    store.export(store.head(ref), tmp_path / "out")
    changed = load_loop_config(tmp_path / "out" / "loop.yaml")
    assert changed == applied(default_loop_config(), [entry])
    assert changed != default_loop_config()


def test_entries_compose_and_hooks_append() -> None:
    entries = [e for e in starter_playbook() if e.id in {"test-after-edit", "repo-map"}]
    loop = applied(default_loop_config(), entries)
    assert loop.context.repo_map
    assert len(loop.hooks) == 1
    assert len(applied(loop, entries).hooks) == 2  # a second merge appends, never replaces


def test_entries_are_ranked_by_how_many_tags_they_match() -> None:
    ranked = for_tags(["gave_up", "check_fail"])
    assert ranked[0].id == "finish-gate-reread"  # matches both
    assert all(e.id != "loop-detection-tight" for e in ranked)
    assert for_tags(["solved"]) == []


def test_a_change_that_changes_nothing_is_no_proposal() -> None:
    loop = applied(default_loop_config(), [starter_playbook()[0]])
    assert proposal_for([starter_playbook()[0]], dump_loop_config(loop), loop) is None


def test_the_tutor_sees_matching_techniques() -> None:
    lines = tutor_lines(["tool_error"])
    assert lines[0].startswith("edit_file accepts old text copied")
    assert "edit_strips_line_numbers: true" in lines[0]
