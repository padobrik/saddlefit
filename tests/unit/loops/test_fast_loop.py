"""Required test 7: the fast loop respects its budget, and probation never reaches main."""

import subprocess
from pathlib import Path

import pytest

from saddlefit.config import FastLoopConfig
from saddlefit.harness.loop_config import LoopConfig, default_loop_config, load_loop_config
from saddlefit.harness.store import MAIN, HarnessStore
from saddlefit.loops.fast import FastLoop, Probe, variant_passes
from saddlefit.playbook import starter_playbook

ENTRY = {e.id: e for e in starter_playbook()}


class Student:
    """Passes only with the settings in ``fixes``; records every attempt it is given."""

    def __init__(self, fixes: set[str], tags: tuple[str, ...] = ("tool_error",)) -> None:
        self.fixes = fixes
        self.tags = tags
        self.loops: list[LoopConfig] = []

    def attempt(self, loop: LoopConfig, seed: int) -> Probe:
        self.loops.append(loop)
        on = {
            "edit": loop.tools.edit_strips_line_numbers,
            "read": not loop.tools.read_line_numbers,
            "map": loop.context.repo_map,
        }
        passed = all(on[f] for f in self.fixes)
        return Probe(f"run{len(self.loops)}", passed, () if passed else self.tags, f"diff{seed}")


def fast(student: Student, **budget: float) -> FastLoop:
    return FastLoop(
        budget=FastLoopConfig(**budget),  # type: ignore[arg-type]
        loop=default_loop_config(),
        attempt=student.attempt,
    )


def test_a_passing_first_attempt_probes_nothing() -> None:
    student = Student(set())
    result = fast(student).run()
    assert result.passed
    assert not result.rescued
    assert len(student.loops) == 1


def test_the_variants_that_helped_are_composed_and_rerun() -> None:
    student = Student({"edit"})
    result = fast(student, max_variants=3, runs_per_variant=2).run()
    assert result.passed
    assert result.rescued
    assert [e.id for e in result.helped] == ["edit-strips-line-numbers"]
    assert student.loops[-1].tools.edit_strips_line_numbers  # the composed rerun


def test_the_budget_caps_variants_and_runs() -> None:
    student = Student({"edit", "read", "map"})  # needs all three at once
    result = fast(student, max_variants=2, runs_per_variant=3).run()
    # one attempt, then at most two variants of three runs; nothing helped, so no rerun
    assert len(student.loops) == 1 + 2 * 3
    assert not result.passed
    assert result.stopped == "no variant helped"
    assert result.diff == "diff7"  # the best attempt, flagged as failed


def test_the_time_budget_stops_probing() -> None:
    now = [0.0]
    student = Student({"map"})

    def slow_attempt(loop: LoopConfig, seed: int) -> Probe:
        now[0] += 6 * 60  # every run takes six minutes
        return student.attempt(loop, seed)

    loop = FastLoop(
        budget=FastLoopConfig(max_variants=3, runs_per_variant=2, time_budget_minutes=10),
        loop=default_loop_config(),
        attempt=slow_attempt,
        clock=lambda: now[0],
    )
    result = loop.run()
    assert len(student.loops) == 2  # the first attempt and one probe, then time is up
    assert result.stopped == "time budget"


def refs(store: HarnessStore) -> str:
    return subprocess.run(
        ["git", "for-each-ref"], cwd=store.path, check=True, capture_output=True, text=True
    ).stdout


def test_probation_changes_never_reach_the_harness_store(tmp_path: Path) -> None:
    store = HarnessStore(tmp_path / "harness")
    store.init()
    main_before = store.head(MAIN)
    refs_before = refs(store)
    loop = load_loop_config(store.path / "loop.yaml")
    student = Student({"edit"})
    result = FastLoop(budget=FastLoopConfig(), loop=loop, attempt=student.attempt).run()
    assert result.rescued
    assert store.head(MAIN) == main_before
    assert load_loop_config(store.path / "loop.yaml") == loop
    assert refs(store) == refs_before  # no candidate branch either


@pytest.mark.parametrize(
    ("passes", "runs", "ok"),
    [
        ([True, True], 2, True),
        ([True, False], 2, False),
        ([True], 1, True),
        ([True, True, False], 3, True),
        ([True, False, False], 3, False),
        ([True, True], 3, True),  # two of three is a majority even if time cut the third
    ],
)
def test_a_variant_passes_by_the_b12_rule(passes: list[bool], runs: int, ok: bool) -> None:
    assert variant_passes(passes, runs) is ok


def test_entries_are_real_playbook_entries() -> None:
    assert {"edit-strips-line-numbers", "read-without-line-numbers", "repo-map"} <= set(ENTRY)
