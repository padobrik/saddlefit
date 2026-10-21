"""The tutor's diagnosis call: prompts (snapshot-tested), run digests and proposals (B13)."""

import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from saddlefit.eval.leak_guard import GuardedClient, LeakDetected, LeakGuard
from saddlefit.providers.base import (
    GenParams,
    Message,
    ModelInfo,
    ModelReply,
    ToolCall,
    ToolSpec,
    Usage,
)
from saddlefit.storage.runs import RunEvent, RunSummary
from saddlefit.tutor.diagnose import (
    PROPOSE_TOOL,
    RunDigest,
    TutorError,
    TutorRequest,
    diagnose,
    digest_run,
    render_runs,
    render_system,
)

SNAPSHOTS = Path(__file__).parent / "snapshots"
RUN_A = "01JB2Y8Q4V9N1K3D7M5R6T0W2X"
RUN_B = "01JB2Y8Q4V9N1K3D7M5R6T0W2Y"
PATCH = (
    "diff --git a/loop.yaml b/loop.yaml\n--- a/loop.yaml\n+++ b/loop.yaml\n@@ -1 +1 @@\n-a\n+b\n"
)


def event(type: str, **data: Any) -> RunEvent:
    return RunEvent.model_validate(
        {
            "schema_version": 1,
            "type": type,
            "time": datetime(2026, 10, 22, tzinfo=UTC),
            "data": data,
        }
    )


def summary(run_id: str, split: str = "train", **changes: Any) -> RunSummary:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "type": "summary",
        "run_id": run_id,
        "task_id": "fix-date-parser-0042",
        "split": split,
        "attempt": 1,
        "model": {"id": "qwen3:8b"},
        "harness_commit": "a1b2c3d",
        "saddlefit_version": "0.2.0",
        "suite_hash": "sha256:x",
        "sandbox_image": "sha256:y",
        "seed": 7,
        "steps": 4,
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "harness_tokens": 300,
        "wall_seconds": 60.0,
        "outcome": "fail",
        "primary_tag": "gave_up",
        "tags": ["gave_up"],
        "diff_stats": {"files": 1, "insertions": 3, "deletions": 1},
    }
    return RunSummary.model_validate({**fields, **changes})


EVENTS = [
    event("model_call", step=1, prompt_tokens=900, completion_tokens=40, tool_calls=["read_file"]),
    event("tool_call", name="read_file", ok=True, output_chars=812),
    event("model_call", step=2, prompt_tokens=950, completion_tokens=60, tool_calls=["edit_file"]),
    event("tool_call", name="edit_file", ok=False, output_chars=40),
    event("loop_warning", signature="edit_file x3", stop=False),
    event("compaction", dropped_outputs=2),
    event("finish_gate", passed=False, output="FAILED tests/test_dates.py::test_iso - ValueError"),
    event(
        "model_call", step=3, prompt_tokens=990, completion_tokens=10, tool_calls=[], malformed=1
    ),
    event("error", kind="gave_up", message="finished without a passing check"),
]


def request(**changes: Any) -> TutorRequest:
    fields: dict[str, Any] = {
        "runs": [
            digest_run(EVENTS, summary(RUN_A), "Fix the ISO date parser.\n"),
            digest_run(EVENTS[:2], summary(RUN_B, diff_stats=None), "Fix the ISO date parser."),
        ],
        "harness_files": {
            "systemprompt.md": "You are a careful software engineer.\n",
            "loop.yaml": "schema_version: 1\nmax_steps: 40\n",
        },
        "experience": "",
        "context_budget": 16000,
        "playbook": ["finish gate: run the visible tests"],
    }
    return TutorRequest(**{**fields, **changes})


def check_snapshot(name: str, text: str) -> None:
    """Compare with ``snapshots/<name>``; ``SADDLEFIT_UPDATE_SNAPSHOTS=1`` rewrites it."""
    path = SNAPSHOTS / name
    if os.environ.get("SADDLEFIT_UPDATE_SNAPSHOTS") == "1":
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8")


def test_system_prompt_snapshot() -> None:
    check_snapshot("system.md", render_system(request()))


def test_runs_prompt_snapshot() -> None:
    check_snapshot("runs.md", render_runs(request()))


def test_a_digest_keeps_the_last_30_events_and_2000_check_characters() -> None:
    events = [event("tool_call", name=f"t{i}", ok=True, output_chars=i) for i in range(40)]
    events.append(event("finish_gate", passed=False, output="x" * 2500 + "END"))
    digest = digest_run(events, summary(RUN_A), "task")
    assert len(digest.events) == 30
    assert digest.events[0] == "tool t11: ok, 11 chars"
    assert len(digest.check_output) == 2000
    assert digest.check_output.endswith("END")


def test_a_run_without_a_visible_check_says_so() -> None:
    assert digest_run(EVENTS[:2], summary(RUN_A), "t").check_output == "(the student never ran it)"


def test_passing_runs_are_not_diagnosed() -> None:
    with pytest.raises(ValueError, match="failed runs only"):
        digest_run(EVENTS, summary(RUN_A, outcome="pass", primary_tag="solved"), "t")


@pytest.mark.parametrize("split", ["held_out", "control"])
def test_only_training_runs_reach_the_tutor(split: str) -> None:
    with pytest.raises(ValueError, match="training runs only"):
        digest_run(EVENTS, summary(RUN_A, split=split), "t")


class Scripted:
    """A ``ModelClient`` that returns one prepared reply and records what it was sent."""

    def __init__(self, reply: ModelReply) -> None:
        self.reply = reply
        self.sent: list[tuple[Sequence[Message], Sequence[ToolSpec] | None]] = []

    def info(self) -> ModelInfo:
        return ModelInfo(provider="fake", model="tutor-model")

    def chat(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec] | None, params: GenParams
    ) -> ModelReply:
        self.sent.append((messages, tools))
        return self.reply


def proposing(**changes: Any) -> ModelReply:
    arguments = {
        "level": "mechanics",
        "failure_tags": ["gave_up"],
        "evidence_run_ids": [RUN_A, RUN_B],
        "technique": "finish gate: run the visible tests",
        "rationale": "Both runs finished without a passing visible check.",
        "patch": PATCH,
        "expected_effect": "Fewer gave_up failures",
        "context_tokens_delta": 0,
        **changes,
    }
    call = ToolCall(id="t1", name="propose", arguments=arguments)
    return ModelReply(tool_calls=(call,), usage=Usage(prompt_tokens=50, completion_tokens=200))


def test_one_call_yields_one_tutor_proposal() -> None:
    client = Scripted(proposing())
    result = diagnose(client, request())
    proposal = result.proposal
    assert proposal.source == "tutor"
    assert proposal.level == "mechanics"
    assert proposal.evidence_run_ids == (RUN_A, RUN_B)
    assert len(proposal.proposal_id) == 26
    assert result.usage.completion_tokens == 200
    assert result.model == "tutor-model"
    messages, tools = client.sent[0]
    assert [m.role for m in messages] == ["system", "user"]
    assert tools == [PROPOSE_TOOL]


def test_proposal_ids_are_unique() -> None:
    first = diagnose(Scripted(proposing()), request()).proposal.proposal_id
    assert diagnose(Scripted(proposing()), request()).proposal.proposal_id != first


@pytest.mark.parametrize(
    ("reply", "match"),
    [
        (
            ModelReply(text="I would add a finish gate.", usage=Usage(completion_tokens=9)),
            r"got \[\]",
        ),
        (
            ModelReply(tool_calls=proposing().tool_calls * 2, usage=Usage(completion_tokens=9)),
            "got",
        ),
        (
            ModelReply(
                tool_calls=(*proposing().tool_calls, ToolCall(id="t2", name="read", arguments={})),
                usage=Usage(completion_tokens=9),
            ),
            "'read'",
        ),
        (
            ModelReply(
                tool_calls=proposing().tool_calls,
                malformed=('{"tool": "propose", "args": {',),
                usage=Usage(completion_tokens=9),
            ),
            "<malformed>",
        ),
        (proposing(evidence_run_ids="not-a-list"), "malformed"),
        (proposing(evidence_run_ids=[{"id": 1}]), "malformed"),
        (proposing(evidence_run_ids=[RUN_A, "01JB2Y8Q4V9N1K3D7M5R6T0W30"]), "not shown"),
        (proposing(evidence_run_ids=[]), "malformed"),
        (proposing(level="budget"), "malformed"),
    ],
)
def test_unusable_answers_are_tutor_errors_with_their_cost(reply: ModelReply, match: str) -> None:
    with pytest.raises(TutorError, match=match) as caught:
        diagnose(Scripted(reply), request())
    assert caught.value.usage == reply.usage


@pytest.mark.parametrize("count", [0, 7])
def test_the_tutor_takes_one_to_six_runs(count: int) -> None:
    run = digest_run(EVENTS, summary(RUN_A), "t")
    with pytest.raises(ValueError, match="1 to 6"):
        diagnose(Scripted(proposing()), request(runs=[run] * count))


def test_a_canary_in_the_tutor_prompt_aborts_the_call() -> None:
    """Required test 2, tutor side: the tutor's prompts go through the leak guard too."""
    canary = "saddlefit-canary-7f3a9c"
    guarded = GuardedClient(Scripted(proposing()), LeakGuard({canary: "fix-date-parser-0042"}))
    leaky = request(harness_files={"EXPERIENCE.md": f"# Lessons\n{canary}\n"})
    with pytest.raises(LeakDetected):
        diagnose(guarded, leaky)


def test_run_digests_carry_no_field_for_hidden_output() -> None:
    """Required test 13, tutor side: a digest holds the visible check only."""
    assert set(RunDigest.__dataclass_fields__) == {
        "run_id",
        "task_text",
        "outcome",
        "primary_tag",
        "tags",
        "diff_stats",
        "events",
        "check_output",
    }
