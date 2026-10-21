"""Reflection into EXPERIENCE.md: no evidence, no lesson; two lines at most; labelled (B13)."""

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from saddlefit.context.assembly import rank_lessons
from saddlefit.harness.proposals import Proposal
from saddlefit.harness.store import HarnessStore, HarnessStoreError
from saddlefit.providers.base import (
    GenParams,
    Message,
    ModelInfo,
    ModelReply,
    ToolCall,
    ToolSpec,
    Usage,
)
from saddlefit.tutor.diagnose import TutorError
from saddlefit.tutor.reflect import (
    Decision,
    Lesson,
    lesson_patch,
    record_lesson,
    reflect,
    render_decision,
)

SNAPSHOTS = Path(__file__).parent / "snapshots"
RUN_A = "01JB2Y8Q4V9N1K3D7M5R6T0W2X"
RUN_B = "01JB2Y8Q4V9N1K3D7M5R6T0W2Y"
PROPOSAL = Proposal(
    schema_version=1,
    proposal_id="01JB2Z0C7H2P6S8E4F1A9B3D5G",
    source="tutor",
    level="mechanics",
    failure_tags=("gave_up",),
    evidence_run_ids=(RUN_A, RUN_B),
    technique="finish gate: allow three retries",
    rationale="Both runs ended after one failed visible check.",
    patch="diff --git a/loop.yaml b/loop.yaml\n",
    expected_effect="Fewer gave_up failures",
    context_tokens_delta=0,
)
ACCEPTED = Decision(
    PROPOSAL, "accepted", train_before=2, train_after=5, train_tasks=6, held_out_net=2
)


class Scripted:
    def __init__(self, reply: ModelReply) -> None:
        self.reply = reply

    def info(self) -> ModelInfo:
        return ModelInfo(provider="fake", model="tutor-model")

    def chat(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec] | None, params: GenParams
    ) -> ModelReply:
        return self.reply


def answer(lesson: str, evidence: Any = (RUN_A,)) -> ModelReply:
    call = ToolCall(
        id="t1",
        name="record_lesson",
        arguments={"lesson": lesson, "evidence_run_ids": list(evidence)},
    )
    return ModelReply(tool_calls=(call,), usage=Usage(prompt_tokens=10, completion_tokens=30))


class NoLint:
    def lint(self, scripts: Mapping[str, bytes]) -> list[str]:
        return []


@pytest.fixture
def store(tmp_path: Path) -> HarnessStore:
    store = HarnessStore(tmp_path / "harness")
    store.init()
    return store


def test_decision_prompt_snapshot() -> None:
    text = (
        render_decision(ACCEPTED)
        + "\n---\n"
        + render_decision(
            Decision(PROPOSAL, "rejected", train_before=2, train_after=2, train_tasks=6)
        )
    )
    path = SNAPSHOTS / "decision.md"
    if os.environ.get("SADDLEFIT_UPDATE_SNAPSHOTS") == "1":
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8")


def test_a_two_line_lesson_becomes_one_labelled_line(store: HarnessStore) -> None:
    text = (
        "More finish-gate retries turned gave_up runs into fixes.\n(train 2/6 → 5/6, held-out +2)"
    )
    result = reflect(Scripted(answer(text)), ACCEPTED, "# Lessons\n")
    assert result.lesson == Lesson(
        text=(
            "More finish-gate retries turned gave_up runs into fixes."
            " (train 2/6 → 5/6, held-out +2)"
        ),
        evidence_run_ids=(RUN_A,),
    )
    assert result.model == "tutor-model"
    before = store.head()
    after = record_lesson(store, result.lesson, "qwen3:8b", NoLint())
    assert after != before
    assert store.head() == after
    experience = store.read("EXPERIENCE.md")
    assert experience.splitlines()[-1].startswith("- (model: qwen3:8b) More finish-gate retries")
    # the student's context picks the lesson for its own model only
    assert len(rank_lessons(experience, "qwen3:8b", "fix a parser")) == 1
    assert rank_lessons(experience, "llama3:8b", "fix a parser") == []


@pytest.mark.parametrize(
    "reply",
    [answer(""), answer("   \n  "), answer("A real lesson.", evidence=())],
    ids=["declined", "blank", "no-evidence"],
)
def test_no_evidence_no_lesson(reply: ModelReply) -> None:
    result = reflect(Scripted(reply), ACCEPTED, "")
    assert result.lesson is None
    assert result.usage == reply.usage


@pytest.mark.parametrize(
    ("reply", "match"),
    [
        (answer("one\ntwo\nthree"), "at most 2 lines"),
        (answer("x" * 401), "400 characters"),
        (answer("A lesson.", evidence=["01JB2Y8Q4V9N1K3D7M5R6T0W30"]), "outside the proposal"),
        (ModelReply(text="Nothing to add.", usage=Usage(completion_tokens=4)), "got"),
        (
            ModelReply(
                tool_calls=(
                    *answer("A lesson.").tool_calls,
                    ToolCall(id="t2", name="x", arguments={}),
                ),
                usage=Usage(completion_tokens=4),
            ),
            "'x'",
        ),
        (
            ModelReply(
                tool_calls=(ToolCall(id="t1", name="record_lesson", arguments={"lesson": 3}),),
                usage=Usage(completion_tokens=4),
            ),
            "malformed",
        ),
    ],
)
def test_unusable_lessons_are_tutor_errors(reply: ModelReply, match: str) -> None:
    with pytest.raises(TutorError, match=match) as caught:
        reflect(Scripted(reply), ACCEPTED, "")
    assert caught.value.usage == reply.usage


@pytest.mark.parametrize(
    "old",
    ["# Lessons\n", "# Lessons\n- (model: m) First.", "", "# Lessons\n- (model: m) a\x0cb\n"],
)
def test_a_lesson_patch_applies_with_git(tmp_path: Path, old: str) -> None:
    (tmp_path / "EXPERIENCE.md").write_bytes(old.encode())  # bytes: no CRLF on Windows
    patch = lesson_patch(old, "- (model: m) Second.")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(
        ["git", "apply", "--recount", "-"],
        input=patch.encode(),
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
    )
    new = (tmp_path / "EXPERIENCE.md").read_bytes().decode()
    assert new == old + ("\n" if old and not old.endswith("\n") else "") + "- (model: m) Second.\n"


def test_a_lesson_already_recorded_is_not_repeated(store: HarnessStore) -> None:
    lesson = Lesson(text="Retries help.", evidence_run_ids=(RUN_A,))
    first = record_lesson(store, lesson, "m", NoLint())
    assert record_lesson(store, lesson, "m", NoLint()) is None
    assert store.head() == first
    assert store.read("EXPERIENCE.md").count("Retries help.") == 1
    # the same text learned on another model is a different lesson
    assert record_lesson(store, lesson, "other", NoLint()) is not None


def test_lessons_only_change_experience_md(store: HarnessStore) -> None:
    with pytest.raises(HarnessStoreError, match="not a harness file"):
        store.read("harness.json")
    with pytest.raises(HarnessStoreError, match="not a harness file"):
        store.read("../saddlefit.yaml")
