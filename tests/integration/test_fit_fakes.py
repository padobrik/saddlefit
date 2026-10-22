"""Required test 6 (fit part): end to end with fakes, the middle loop accepts the one change
that helps, rejects a useless one, and never evaluates control (A7, B12, B13)."""

import re
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from saddlefit.config import TokenPrices
from saddlefit.eval.decisions import HeldOutComparison
from saddlefit.harness.store import HarnessStore, HarnessStoreError
from saddlefit.loops.fit import run_rounds
from saddlefit.loops.middle import BatchResult, MiddleLoop, pick_batch
from saddlefit.providers.base import (
    GenParams,
    Message,
    ModelInfo,
    ModelReply,
    ToolCall,
    ToolSpec,
    Usage,
)
from saddlefit.storage.fit_state import load_fit_state
from saddlefit.storage.library import read_records
from saddlefit.storage.runs import RunEvent, RunSummary
from saddlefit.tutor.diagnose import digest_run

TRAIN = [f"t{i}" for i in range(6)]
HELD_OUT = [f"h{i}" for i in range(6)]
CONTROL = [f"c{i}" for i in range(4)]
# a run ID per training task, so proposals can cite them
RUN_IDS = {t: f"01JB2Y8Q4V9N1K3D7M5R6T0W{i:02d}" for i, t in enumerate(TRAIN)}


def retries(store: HarnessStore, ref: str) -> int:
    return int(yaml.safe_load(store.read("loop.yaml", ref))["finish_gate"]["max_retries"])


def solves(task: str, max_retries: int) -> bool:
    """The fake student: the first task always, the rest only with a patient finish gate."""
    return task in {"t0", "h0"} or max_retries >= 3


class FakePractice:
    """A student that only gets better with one specific harness change."""

    runs_per_task = 3

    def __init__(self, store: HarnessStore) -> None:
        self.store = store
        self.asked: list[str] = []
        self.held_out_comparisons = 0

    def run_batch(self, harness_ref: str, task_ids: Sequence[str]) -> BatchResult:
        self.asked += task_ids
        n = retries(self.store, harness_ref)
        solved = {t: solves(t, n) for t in task_ids}
        failures = tuple(self._failure(t) for t in task_ids if not solved[t])
        return BatchResult(solved=solved, failures=failures)

    def compare_held_out(self, current_ref: str, candidate_ref: str) -> HeldOutComparison:
        self.held_out_comparisons += 1
        before = {t: solves(t, retries(self.store, current_ref)) for t in HELD_OUT}
        after = {t: solves(t, retries(self.store, candidate_ref)) for t in HELD_OUT}
        wins = sum(after[t] and not before[t] for t in HELD_OUT)
        losses = sum(before[t] and not after[t] for t in HELD_OUT)
        return HeldOutComparison(wins=wins, losses=losses, tasks=len(HELD_OUT))

    def _failure(self, task: str) -> Any:
        event = RunEvent.model_validate(
            {
                "schema_version": 1,
                "type": "finish_gate",
                "time": datetime(2026, 10, 22, tzinfo=UTC),
                "data": {"passed": False, "output": "1 failed"},
            }
        )
        summary = RunSummary.model_validate(
            {
                "schema_version": 1,
                "type": "summary",
                "run_id": RUN_IDS[task],
                "task_id": task,
                "split": "train",
                "attempt": 1,
                "model": {"id": "student"},
                "harness_commit": "a1b2c3d",
                "saddlefit_version": "0.2.0",
                "suite_hash": "sha256:x",
                "sandbox_image": "sha256:y",
                "seed": 7,
                "steps": 3,
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "harness_tokens": 50,
                "wall_seconds": 1.0,
                "outcome": "fail",
                "primary_tag": "gave_up",
                "tags": ["gave_up"],
            }
        )
        return digest_run([event], summary, f"Fix task {task}.")


USELESS = (
    "diff --git a/systemprompt.md b/systemprompt.md\n--- a/systemprompt.md\n+++ b/systemprompt.md\n"
    "@@ -1 +1 @@\n-You are a careful software engineer. Solve the task, then run its check.\n"
    "+You are a very careful software engineer. Solve the task, then run its check.\n"
)
RIGHT = (
    "diff --git a/loop.yaml b/loop.yaml\n--- a/loop.yaml\n+++ b/loop.yaml\n"
    "@@ -10,3 +10,3 @@\n   run: pytest -q\n-  max_retries: 1\n+  max_retries: 3\n loop_detection:\n"
)


class FakeTutor:
    """Replays proposals in order; answers every reflection with a lesson."""

    def __init__(self, patches: Sequence[str]) -> None:
        self.patches = list(patches)
        self.prompts: list[str] = []

    def info(self) -> ModelInfo:
        return ModelInfo(provider="fake", model="fake-tutor")

    def chat(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec] | None, params: GenParams
    ) -> ModelReply:
        self.prompts += [m.content for m in messages]
        assert tools is not None
        if tools[0].name == "record_lesson":
            args: Mapping[str, Any] = {
                "lesson": "A patient finish gate fixed gave_up runs (train 1/6 → 6/6).",
                "evidence_run_ids": [RUN_IDS["t1"]],
            }
        else:
            args = {
                "level": "mechanics",
                "failure_tags": ["gave_up"],
                "evidence_run_ids": [RUN_IDS["t1"], RUN_IDS["t2"]],
                "technique": f"candidate {len(self.patches)}",
                "rationale": "Runs end after one failed visible check.",
                "patch": self.patches.pop(0),
                "expected_effect": "Fewer gave_up failures",
                "context_tokens_delta": 0,
            }
        call = ToolCall(id="t", name=tools[0].name, arguments=dict(args))
        return ModelReply(
            tool_calls=(call,), usage=Usage(prompt_tokens=1000, completion_tokens=200)
        )


class NoLint:
    def lint(self, scripts: Mapping[str, bytes]) -> list[str]:
        return []


def make_loop(tmp_path: Path, tutor: FakeTutor, **changes: Any) -> tuple[MiddleLoop, FakePractice]:
    store = HarnessStore(tmp_path / "harness")
    store.init()
    practice = FakePractice(store)
    fields: dict[str, Any] = {
        "store": store,
        "practice": practice,
        "tutor": tutor,
        "linter": NoLint(),
        "root": tmp_path,
        "train_tasks": TRAIN,
        "student_model": "student",
        "student_digest": None,
        "context_budget": 16000,
        "max_calls_per_round": 20,
        "max_usd_per_round": 6.0,
        "prices": TokenPrices(input=4, output=20),
        "candidates_per_round": 2,
        "today": lambda: date(2026, 10, 22),
    }
    return MiddleLoop(**{**fields, **changes}), practice


def test_fit_accepts_the_change_that_helps_and_rejects_the_useless_one(tmp_path: Path) -> None:
    tutor = FakeTutor([USELESS, RIGHT])
    loop, practice = make_loop(tmp_path, tutor)
    report = loop.round(0)

    assert report.train_before == 1
    useless, right = report.records
    assert (useless.status, right.status) == ("rejected", "accepted")
    assert report.accepted == right.proposal_id
    assert retries(loop.store, "main") == 3
    assert "very careful" not in loop.store.read("systemprompt.md")
    # the useless change failed the batch gate, so it never reached held-out tasks
    assert useless.held_out is None
    assert right.held_out is not None
    assert (right.held_out.wins, right.held_out.losses) == (5, 0)
    assert (right.train.before, right.train.after) == (1, 6)
    assert [r.id for r in read_records(tmp_path)] == ["2026-10-22-001", "2026-10-22-002"]
    # one lesson per decision, labelled with the student model
    assert report.lessons == 1  # the second lesson repeats the first and is skipped
    assert "- (model: student) A patient finish gate" in loop.store.read("EXPERIENCE.md")
    # no control evaluation, and the tutor never sees held-out or control task IDs
    assert set(practice.asked) <= set(TRAIN)
    assert practice.held_out_comparisons == 1
    assert not hasattr(practice, "run_control")
    prompts = "\n".join(tutor.prompts)
    assert not any(re.search(rf"\b{t}\b", prompts) for t in HELD_OUT + CONTROL)
    # 2 diagnoses + 2 reflections, priced
    assert report.tutor_calls == 4
    assert report.tutor_usd == pytest.approx(4 * (1000 * 4 + 200 * 20) / 1e6)


def test_a_round_without_failures_asks_the_tutor_nothing(tmp_path: Path) -> None:
    tutor = FakeTutor([])
    loop, _ = make_loop(tmp_path, tutor, train_tasks=["t0"])
    report = loop.round(0)
    assert report.records == []
    assert report.tutor_calls == 0


def test_the_budget_caps_tutor_calls(tmp_path: Path) -> None:
    tutor = FakeTutor([USELESS, USELESS, USELESS])
    loop, _ = make_loop(tmp_path, tutor, candidates_per_round=3, max_calls_per_round=1)
    report = loop.round(0)
    assert report.tutor_calls == 1
    assert len(report.records) == 1


def test_the_spend_caps_tutor_calls(tmp_path: Path) -> None:
    tutor = FakeTutor([USELESS, USELESS, USELESS])
    # one call costs $0.008; a $0.01 cap allows a second call to start, then stops
    loop, _ = make_loop(tmp_path, tutor, candidates_per_round=3, max_usd_per_round=0.01)
    report = loop.round(0)
    assert report.tutor_calls == 2


def test_batches_move_through_the_training_tasks() -> None:
    assert pick_batch(TRAIN, 0, 4) == ("t0", "t1", "t2", "t3")
    assert pick_batch(TRAIN, 1, 4) == ("t4", "t5", "t0", "t1")
    assert pick_batch(TRAIN, 0, 10) == tuple(TRAIN)
    with pytest.raises(ValueError, match="no training tasks"):
        pick_batch([], 0, 4)


class OverfittingPractice(FakePractice):
    """The change helps the training batch but costs two held-out tasks."""

    def compare_held_out(self, current_ref: str, candidate_ref: str) -> HeldOutComparison:
        self.held_out_comparisons += 1
        return HeldOutComparison(wins=1, losses=2, tasks=len(HELD_OUT))


def test_a_change_that_only_helps_training_is_rejected(tmp_path: Path) -> None:
    loop, _ = make_loop(tmp_path, FakeTutor([RIGHT]), candidates_per_round=1)
    loop.practice = OverfittingPractice(loop.store)
    report = loop.round(0)
    (record,) = report.records
    assert record.status == "rejected"
    assert record.held_out is not None
    assert report.accepted is None
    assert retries(loop.store, "main") == 1


def test_no_candidate_branch_outlives_its_round(tmp_path: Path) -> None:
    loop, _ = make_loop(tmp_path, FakeTutor([USELESS, RIGHT]))
    report = loop.round(0)
    for record in report.records:
        with pytest.raises(HarnessStoreError):
            loop.store.head(f"candidate/{record.proposal_id}")


# the winner also edits EXPERIENCE.md, where every lesson is appended
RIGHT_WITH_NOTE = RIGHT + (
    "diff --git a/EXPERIENCE.md b/EXPERIENCE.md\n--- a/EXPERIENCE.md\n+++ b/EXPERIENCE.md\n"
    "@@ -1 +1,2 @@\n # Lessons\n+- (model: student) Keep the finish gate patient.\n"
)


def candidate_branches(store: HarnessStore) -> list[str]:
    out = subprocess.run(
        ["git", "branch", "--list", "candidate/*"],
        cwd=store.path,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


def test_lessons_wait_until_every_candidate_is_decided(tmp_path: Path) -> None:
    # a lesson about the rejected tweak must not reach main before the winner is merged
    loop, _ = make_loop(tmp_path, FakeTutor([USELESS, RIGHT_WITH_NOTE]))
    report = loop.round(0)
    assert report.accepted == report.records[1].proposal_id
    assert "Keep the finish gate patient." in loop.store.read("EXPERIENCE.md")
    assert candidate_branches(loop.store) == []


class CrashingPractice(FakePractice):
    """The second candidate's batch run fails, as a broken sandbox would."""

    def run_batch(self, harness_ref: str, task_ids: Sequence[str]) -> BatchResult:
        if harness_ref != "main" and getattr(self, "candidate_runs", 0) >= 1:
            raise RuntimeError("sandbox died")
        if harness_ref != "main":
            self.candidate_runs = getattr(self, "candidate_runs", 0) + 1
        return super().run_batch(harness_ref, task_ids)


def test_a_failed_round_leaves_no_candidate_branch(tmp_path: Path) -> None:
    loop, _ = make_loop(tmp_path, FakeTutor([USELESS, RIGHT]))
    loop.practice = CrashingPractice(loop.store)
    with pytest.raises(RuntimeError, match="sandbox died"):
        loop.round(0)
    assert candidate_branches(loop.store) == []
    assert retries(loop.store, "main") == 1


def test_a_failed_lesson_does_not_stop_the_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise HarnessStoreError("git apply failed")

    monkeypatch.setattr("saddlefit.loops.middle.record_lesson", broken)
    loop, _ = make_loop(tmp_path, FakeTutor([USELESS, RIGHT]))
    report = loop.round(0)
    assert report.accepted is not None
    assert len(report.records) == 2
    assert report.lessons == 0


DROP_EXPERIENCE = RIGHT + (
    "diff --git a/EXPERIENCE.md b/EXPERIENCE.md\ndeleted file mode 100644\n"
    "--- a/EXPERIENCE.md\n+++ /dev/null\n@@ -1 +0,0 @@\n-# Lessons\n"
)


def test_a_round_survives_a_winner_that_removes_experience_md(tmp_path: Path) -> None:
    loop, _ = make_loop(tmp_path, FakeTutor([DROP_EXPERIENCE]), candidates_per_round=1)
    report = loop.round(0)
    assert report.accepted == report.records[0].proposal_id
    assert report.lessons == 0
    assert "EXPERIENCE.md" not in loop.store.texts()


def test_each_fit_continues_with_the_next_batch(tmp_path: Path) -> None:
    loop, _ = make_loop(
        tmp_path, FakeTutor([USELESS, USELESS]), batch_size=4, candidates_per_round=1
    )
    (first,) = run_rounds(loop, tmp_path, 1)
    (second,) = run_rounds(loop, tmp_path, 1)  # a new fit invocation
    assert first.batch == ("t0", "t1", "t2", "t3")
    assert second.batch == ("t4", "t5", "t0", "t1")
    assert load_fit_state(tmp_path).rounds_done == 2


def test_a_failed_round_is_not_counted(tmp_path: Path) -> None:
    loop, _ = make_loop(tmp_path, FakeTutor([USELESS, RIGHT]))
    loop.practice = CrashingPractice(loop.store)
    with pytest.raises(RuntimeError):
        run_rounds(loop, tmp_path, 1)
    assert load_fit_state(tmp_path).rounds_done == 0


def test_tutor_spending_adds_up_across_fits(tmp_path: Path) -> None:
    loop, _ = make_loop(tmp_path, FakeTutor([USELESS, USELESS]), candidates_per_round=1)
    first = run_rounds(loop, tmp_path, 1)
    second = run_rounds(loop, tmp_path, 1)
    state = load_fit_state(tmp_path)
    calls = sum(r.tutor_calls for r in first + second)
    assert calls > 0
    assert state.tutor_calls == calls
    assert state.tutor_usd == pytest.approx(sum(r.tutor_usd for r in first + second))


def test_a_failed_round_still_records_what_the_tutor_cost(tmp_path: Path) -> None:
    loop, _ = make_loop(tmp_path, FakeTutor([USELESS, RIGHT]))
    loop.practice = CrashingPractice(loop.store)
    with pytest.raises(RuntimeError):
        run_rounds(loop, tmp_path, 1)
    state = load_fit_state(tmp_path)
    assert state.rounds_done == 0
    assert state.tutor_calls == 2  # both diagnoses were paid for before the crash
    assert state.tutor_usd > 0
