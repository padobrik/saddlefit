"""Required test 6 (prune part) and ablation end to end with fakes (B12)."""

from datetime import date
from pathlib import Path

import pytest
from tests.integration.test_fit_fakes import (
    RIGHT,
    USELESS,
    FakeTutor,
    make_loop,
    retries,
)

from saddlefit.harness.store import HarnessStore
from saddlefit.loops.ablation import AblationResult, ablate, active_changes
from saddlefit.loops.fit import run_rounds
from saddlefit.loops.middle import MiddleLoop, RoundReport
from saddlefit.storage.fit_state import FitState, load_fit_state, save_fit_state
from saddlefit.storage.library import (
    ChangeRecord,
    HeldOutCounts,
    TrainCounts,
    next_change_id,
    read_records,
    write_record,
)

NOISE_ID = "01JB2Z0C7H2P6S8E4F1A9B3D5N"


def accept_by_noise(store: HarnessStore, root: Path) -> None:
    """A useless change that slipped through earlier, as a noisy round can let happen."""
    sha = store.propose(NOISE_ID, USELESS, "a lucky tweak")
    store.accept(NOISE_ID)
    write_record(
        root,
        ChangeRecord(
            schema_version=1,
            id=next_change_id(root, date(2026, 10, 21)),
            proposal_id=NOISE_ID,
            model="student",
            level="text",
            files=("systemprompt.md",),
            harness_commit=sha,
            status="accepted",
            train=TrainCounts(before=1, after=2, tasks=6, runs_per_task=3),
            held_out=HeldOutCounts(wins=1, losses=0, tasks=6, runs_per_task=3),
            context_tokens_added=5,
        ),
    )


def test_ablation_prunes_the_useless_change_and_keeps_the_right_one(tmp_path: Path) -> None:
    """Required test 6: prune removes a useless change; the helpful one stays."""
    loop, _ = make_loop(tmp_path, FakeTutor([RIGHT]), candidates_per_round=1)
    accept_by_noise(loop.store, tmp_path)
    assert loop.round(0).accepted is not None
    assert "very careful" in loop.store.read("systemprompt.md")

    results = ablate(loop)

    # newest first: the right change is tested before the noise
    assert [(r.pruned, r.skipped) for r in results] == [(False, None), (True, None)]
    kept, pruned = results
    assert kept.train is not None
    assert kept.train.losses == 5  # removing it costs 5 tasks
    assert pruned.proposal_id == NOISE_ID
    assert "very careful" not in loop.store.read("systemprompt.md")
    assert retries(loop.store, "main") == 3
    (record,) = [r for r in read_records(tmp_path) if r.status == "pruned"]
    assert record.proposal_id == NOISE_ID
    assert record.context_tokens_added == -5
    assert record.harness_commit == loop.store.head()
    assert [r.proposal_id for r in active_changes(read_records(tmp_path))] == [kept.proposal_id]
    # a second ablation finds nothing more to drop
    assert [r.pruned for r in ablate(loop)] == [False]


def test_a_change_others_build_on_is_skipped(tmp_path: Path) -> None:
    loop, _ = make_loop(tmp_path, FakeTutor([]))
    accept_by_noise(loop.store, tmp_path)
    # a later, helpful change also rewrites the line the noise changed, so the noise cannot
    # be removed on its own while the later change stays
    later = RIGHT + (
        "diff --git a/systemprompt.md b/systemprompt.md\n--- a/systemprompt.md\n"
        "+++ b/systemprompt.md\n@@ -1 +1 @@\n"
        "-You are a very careful software engineer. Solve the task, then run its check.\n"
        "+You are a very, very careful software engineer. Solve the task, then run its check.\n"
    )
    later_id = "01JB2Z0C7H2P6S8E4F1A9B3D5P"
    sha = loop.store.propose(later_id, later, "more care, more retries")
    loop.store.accept(later_id)
    write_record(
        tmp_path,
        read_records(tmp_path)[0].model_copy(
            update={"id": "2026-10-21-002", "proposal_id": later_id, "harness_commit": sha}
        ),
    )
    results = {r.proposal_id: r for r in ablate(loop)}
    assert not results[later_id].pruned  # removing it costs training tasks
    assert results[NOISE_ID].skipped is not None  # its revert conflicts with the later change
    assert not results[NOISE_ID].pruned
    assert "very, very careful" in loop.store.read("systemprompt.md")


def test_three_accepted_changes_trigger_ablation(tmp_path: Path) -> None:
    loop, _ = make_loop(tmp_path, FakeTutor([RIGHT]), candidates_per_round=1)
    accept_by_noise(loop.store, tmp_path)
    save_fit_state(
        tmp_path,
        FitState(
            schema_version=1, rounds_done=0, accepted_since_ablation=2, student_model="student"
        ),
    )
    (report,) = run_rounds(loop, tmp_path, 1)  # the third accepted change
    assert report.accepted is not None
    assert report.pruned == [NOISE_ID]
    assert load_fit_state(tmp_path).accepted_since_ablation == 0


def test_no_ablation_before_three_accepted_changes(tmp_path: Path) -> None:
    loop, _ = make_loop(tmp_path, FakeTutor([RIGHT]), candidates_per_round=1)
    accept_by_noise(loop.store, tmp_path)
    (report,) = run_rounds(loop, tmp_path, 1)
    assert report.pruned == []
    assert load_fit_state(tmp_path).accepted_since_ablation == 1
    assert "very careful" in loop.store.read("systemprompt.md")


def test_a_new_student_model_triggers_ablation_first(tmp_path: Path) -> None:
    loop, _ = make_loop(tmp_path, FakeTutor([USELESS]), candidates_per_round=1)
    accept_by_noise(loop.store, tmp_path)
    save_fit_state(
        tmp_path, FitState(schema_version=1, rounds_done=3, student_model="older-student")
    )
    (report,) = run_rounds(loop, tmp_path, 1)
    assert report.pruned == [NOISE_ID]
    assert load_fit_state(tmp_path).student_model == "student"


@pytest.mark.parametrize("model", [None, "student"])
def test_the_same_or_a_first_model_does_not_trigger_ablation(
    tmp_path: Path, model: str | None
) -> None:
    loop, _ = make_loop(tmp_path, FakeTutor([USELESS]), candidates_per_round=1)
    accept_by_noise(loop.store, tmp_path)
    save_fit_state(tmp_path, FitState(schema_version=1, rounds_done=0, student_model=model))
    (report,) = run_rounds(loop, tmp_path, 1)
    assert report.pruned == []


def test_an_interrupted_ablation_resumes_without_repeating_the_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop, _ = make_loop(tmp_path, FakeTutor([RIGHT, USELESS]), candidates_per_round=1)
    accept_by_noise(loop.store, tmp_path)
    save_fit_state(
        tmp_path,
        FitState(
            schema_version=1, rounds_done=0, accepted_since_ablation=2, student_model="student"
        ),
    )

    def broken(loop: object) -> list[object]:
        raise RuntimeError("sandbox died during ablation")

    monkeypatch.setattr("saddlefit.loops.fit.ablate", broken)
    with pytest.raises(RuntimeError, match="during ablation"):
        run_rounds(loop, tmp_path, 1)
    state = load_fit_state(tmp_path)
    assert (state.rounds_done, state.accepted_since_ablation) == (1, 3)  # the round counted

    monkeypatch.undo()
    calls: list[str] = []
    real_round = loop.round

    def spy_ablate(loop: MiddleLoop) -> list[AblationResult]:
        calls.append("ablate")
        return ablate(loop)

    def spy_round(index: int) -> RoundReport:
        calls.append(f"round {index}")
        return real_round(index)

    monkeypatch.setattr("saddlefit.loops.fit.ablate", spy_ablate)
    monkeypatch.setattr(loop, "round", spy_round)
    (report,) = run_rounds(loop, tmp_path, 1)
    assert calls == ["ablate", "round 1"]  # the pending ablation first, then the next round
    assert report.pruned == [NOISE_ID]  # the pending ablation ran first
    assert load_fit_state(tmp_path).rounds_done == 2
