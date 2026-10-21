"""Reflection: after each decision, at most one lesson for EXPERIENCE.md (B13).

"No evidence, no lesson": the tutor may decline, and a lesson that cites no evidence run is
dropped. A lesson is at most two lines, is labelled with the student model it was learned
on, and reaches the harness only as a validated proposal, like any other change (hard
rule 3). It is stored as one line, since the student's context takes each ``- `` line of
EXPERIENCE.md as one lesson and filters lessons by their ``(model: …)`` label.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import ValidationError
from ulid import ULID

from saddlefit.harness.proposals import (
    PROPOSAL_SCHEMA_VERSION,
    Proposal,
    ScriptLinter,
    apply_proposal,
)
from saddlefit.harness.store import HarnessStore
from saddlefit.providers.base import GenParams, Message, ModelClient, ToolSpec, Usage
from saddlefit.tutor.diagnose import TutorError, load_prompt

EXPERIENCE_FILE: Final = "EXPERIENCE.md"
MAX_LINES: Final = 2
MAX_CHARS: Final = 400
TOOL_NAME: Final = "record_lesson"

LESSON_TOOL: Final = ToolSpec(
    name=TOOL_NAME,
    description="Record one lesson for EXPERIENCE.md, or an empty lesson if there is none.",
    parameters={
        "type": "object",
        "properties": {
            "lesson": {"type": "string"},
            "evidence_run_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["lesson", "evidence_run_ids"],
        "additionalProperties": False,
    },
)


@dataclass(frozen=True)
class Decision:
    """What the middle loop decided; held-out data arrives as an aggregate only (B10)."""

    proposal: Proposal
    status: Literal["accepted", "rejected"]
    train_before: int
    train_after: int
    train_tasks: int
    held_out_net: int | None = None  # None when the batch gate already rejected it


@dataclass(frozen=True)
class Lesson:
    text: str  # one line, without the label
    evidence_run_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReflectionResult:
    lesson: Lesson | None
    usage: Usage
    model: str


def render_decision(decision: Decision) -> str:
    p = decision.proposal
    held_out = "not reached" if decision.held_out_net is None else f"net {decision.held_out_net:+d}"
    return load_prompt("decision.md").substitute(
        status=decision.status,
        level=p.level,
        failure_tags=", ".join(p.failure_tags) or "none",
        technique=p.technique,
        rationale=p.rationale,
        expected_effect=p.expected_effect,
        evidence=", ".join(p.evidence_run_ids) or "none",
        train=(
            f"{decision.train_before}/{decision.train_tasks} → "
            f"{decision.train_after}/{decision.train_tasks} tasks solved"
        ),
        held_out=held_out,
    )


def reflect(client: ModelClient, decision: Decision, experience: str) -> ReflectionResult:
    """One tutor call; returns no lesson when the tutor declines or cites no evidence."""
    system = load_prompt("reflect.md").substitute(
        max_chars=MAX_CHARS, experience=experience.strip() or "(empty)"
    )
    messages = [
        Message(role="system", content=system),
        Message(role="user", content=render_decision(decision)),
    ]
    reply = client.chat(messages, [LESSON_TOOL], GenParams())
    served = getattr(reply.raw, "model", None) or client.info().model
    names = [c.name for c in reply.tool_calls]
    if names != [TOOL_NAME] or reply.malformed:
        attempted = names + ["<malformed>"] * len(reply.malformed)
        raise TutorError(f"expected exactly one {TOOL_NAME} call, got {attempted}", reply.usage)
    arguments = reply.tool_calls[0].arguments
    text = arguments.get("lesson")
    evidence = arguments.get("evidence_run_ids")
    if not isinstance(text, str) or not isinstance(evidence, list):
        raise TutorError("the lesson is malformed", reply.usage)
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines or not evidence:  # no evidence, no lesson
        return ReflectionResult(lesson=None, usage=reply.usage, model=served)
    if len(lines) > MAX_LINES or len(" ".join(lines)) > MAX_CHARS:
        raise TutorError(
            f"a lesson is at most {MAX_LINES} lines and {MAX_CHARS} characters", reply.usage
        )
    unknown = sorted({str(e) for e in evidence} - set(decision.proposal.evidence_run_ids))
    if unknown:
        raise TutorError(
            f"the lesson cites runs outside the proposal's evidence: {unknown}", reply.usage
        )
    lesson = Lesson(text=" ".join(lines), evidence_run_ids=tuple(str(e) for e in evidence))
    return ReflectionResult(lesson=lesson, usage=reply.usage, model=served)


def lesson_line(lesson: Lesson, student_model: str) -> str:
    return f"- (model: {student_model}) {lesson.text}"


def lesson_patch(experience: str, line: str) -> str:
    """A git-style diff that appends ``line`` to EXPERIENCE.md."""
    after = (
        experience + ("\n" if experience and not experience.endswith("\n") else "") + line + "\n"
    )
    body = difflib.unified_diff(
        experience.splitlines(keepends=True),
        after.splitlines(keepends=True),
        f"a/{EXPERIENCE_FILE}",
        f"b/{EXPERIENCE_FILE}",
    )
    # a diff line without its newline is an old last line that had none
    text = "".join(
        chunk if chunk.endswith("\n") else chunk + "\n\\ No newline at end of file\n"
        for chunk in body
    )
    return f"diff --git a/{EXPERIENCE_FILE} b/{EXPERIENCE_FILE}\n{text}"


def record_lesson(
    store: HarnessStore, lesson: Lesson, student_model: str, linter: ScriptLinter
) -> str:
    """Append the lesson to EXPERIENCE.md on ``main`` through a validated proposal."""
    line = lesson_line(lesson, student_model)
    try:
        proposal = Proposal(
            schema_version=PROPOSAL_SCHEMA_VERSION,
            proposal_id=str(ULID()),
            source="tutor",
            level="text",
            failure_tags=(),
            evidence_run_ids=lesson.evidence_run_ids,
            technique="lesson for EXPERIENCE.md",
            rationale=lesson.text,
            patch=lesson_patch(store.read(EXPERIENCE_FILE), line),
            expected_effect="later rounds and the student know what worked",
            context_tokens_delta=len(line) // 4,
        )
    except ValidationError as exc:
        raise TutorError(f"the lesson cannot become a proposal: {exc}") from exc
    apply_proposal(store, proposal, linter)
    return store.accept(proposal.proposal_id)
