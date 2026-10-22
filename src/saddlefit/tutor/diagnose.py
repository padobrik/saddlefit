"""Diagnose failed training runs and get exactly one proposal per call (B13).

The tutor sees up to six failed training runs, each compressed to the task text, the
verdict and failure tags, the last 30 events, the diff statistics and the end of the
visible check's output from the student's own run. It never sees hidden-check output,
check scripts, reference solutions or held-out and control data: run records hold none of
them, and ``read_run`` refuses records outside the training split.

The stable part of the prompt (instructions, constraints, harness files, lessons and
playbook) is the system prompt, which the provider caches; the runs follow as the user
message. Prompts are Markdown files in ``prompts/`` with ``$name`` placeholders.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from string import Template
from typing import Any, Final, get_args

from pydantic import ValidationError
from ulid import ULID

from saddlefit.eval.taxonomy import Tag
from saddlefit.harness.allowlist import ALLOWED_DIRS, ALLOWED_FILES
from saddlefit.harness.proposals import PROPOSAL_SCHEMA_VERSION, Proposal
from saddlefit.harness.store import MAX_PATCH_LINES
from saddlefit.providers.base import (
    GenParams,
    Message,
    ModelClient,
    ProviderError,
    ToolSpec,
    Usage,
)
from saddlefit.storage.runs import FinishGateData, RunEvent, RunSummary

MAX_RUNS: Final = 6
LAST_EVENTS: Final = 30
CHECK_CHARS: Final = 2_000
LEVEL_ORDER: Final = ("mechanics", "tools", "text", "reasoning")
TOOL_NAME: Final = "propose"

PROPOSE_TOOL: Final = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Submit exactly one change to the harness. `patch` is a git-style unified diff: "
        "each file starts with `diff --git a/<path> b/<path>`."
    ),
    parameters={
        "type": "object",
        "properties": {
            "level": {"type": "string", "enum": list(LEVEL_ORDER)},
            "failure_tags": {
                "type": "array",
                "items": {"type": "string", "enum": list(get_args(Tag))},
            },
            "evidence_run_ids": {"type": "array", "items": {"type": "string"}},
            "technique": {"type": "string"},
            "rationale": {"type": "string"},
            "patch": {"type": "string"},
            "expected_effect": {"type": "string"},
            "context_tokens_delta": {"type": "integer"},
        },
        "required": [
            "level",
            "failure_tags",
            "evidence_run_ids",
            "technique",
            "rationale",
            "patch",
            "expected_effect",
            "context_tokens_delta",
        ],
        "additionalProperties": False,
    },
)


def load_prompt(name: str) -> Template:
    return Template((resources.files("saddlefit.tutor") / "prompts" / name).read_text("utf-8"))


@dataclass(frozen=True)
class RunDigest:
    """One failed training run as the tutor sees it."""

    run_id: str
    task_text: str
    outcome: str
    primary_tag: str
    tags: tuple[str, ...]
    diff_stats: str
    events: tuple[str, ...]
    check_output: str


def _event_line(event: RunEvent) -> str:
    data: Any = event.data
    match event.type:
        case "model_call":
            calls = ", ".join(data.tool_calls) or "no tool calls"
            extra = f", {data.malformed} malformed" if data.malformed else ""
            return f"model step {data.step}: {calls}{extra}"
        case "tool_call":
            return f"tool {data.name}: {'ok' if data.ok else 'error'}, {data.output_chars} chars"
        case "compaction":
            return f"compaction: dropped {data.dropped_outputs} tool outputs"
        case "loop_warning":
            return f"loop warning{' (stopped)' if data.stop else ''}: {data.signature}"
        case "finish_gate":
            return f"finish gate: visible check {'passed' if data.passed else 'failed'}"
        case _:
            return f"error {data.kind}: {data.message}"


def digest_run(events: Sequence[RunEvent], summary: RunSummary, task_text: str) -> RunDigest:
    """Compress a training run; the record must come from ``read_run`` (train split only)."""
    if summary.split != "train":
        raise ValueError(f"the tutor sees training runs only, not {summary.split}")
    if summary.outcome != "fail":
        raise ValueError(f"the tutor diagnoses failed runs only; {summary.run_id} passed")
    gates = [e.data.output for e in events if isinstance(e.data, FinishGateData)]
    diff = summary.diff_stats
    return RunDigest(
        run_id=summary.run_id,
        task_text=task_text.strip(),
        outcome=summary.outcome,
        primary_tag=summary.primary_tag,
        tags=summary.tags,
        diff_stats=(
            f"{diff.files} files, +{diff.insertions} -{diff.deletions}" if diff else "no changes"
        ),
        events=tuple(_event_line(e) for e in events[-LAST_EVENTS:]),
        check_output=gates[-1][-CHECK_CHARS:] if gates else "(the student never ran it)",
    )


@dataclass(frozen=True)
class TutorRequest:
    runs: Sequence[RunDigest]
    harness_files: Mapping[str, str]
    experience: str
    context_budget: int
    playbook: Sequence[str] = field(default_factory=tuple)


def render_system(request: TutorRequest) -> str:
    files = "\n\n".join(
        f"## {path}\n\n```\n{text.rstrip()}\n```"
        for path, text in sorted(request.harness_files.items())
    )
    allowlist = sorted(ALLOWED_FILES) + [f"{d}/**" for d in sorted(ALLOWED_DIRS)]
    return load_prompt("system.md").substitute(
        level_order=", ".join(LEVEL_ORDER),
        allowlist=", ".join(f"`{p}`" for p in allowlist),
        max_patch_lines=MAX_PATCH_LINES,
        context_budget=request.context_budget,
        experience=request.experience.strip() or "(none yet)",
        playbook="\n".join(f"- {t}" for t in request.playbook) or "(none yet)",
        harness_files=files,
    )


def render_runs(request: TutorRequest) -> str:
    run = load_prompt("run.md")
    runs = "\n\n".join(
        run.substitute(
            run_id=d.run_id,
            outcome=d.outcome,
            primary_tag=d.primary_tag,
            tags=", ".join(f"`{t}`" for t in d.tags),
            diff_stats=d.diff_stats,
            task_text=d.task_text,
            event_count=len(d.events),
            events="\n".join(f"{i}. {line}" for i, line in enumerate(d.events, 1)),
            check_chars=CHECK_CHARS,
            check_output=d.check_output,
        ).rstrip()
        for d in request.runs
    )
    return load_prompt("diagnose.md").substitute(run_count=len(request.runs), runs=runs)


class TutorError(ProviderError):
    """The tutor answered without a usable proposal; ``usage`` holds what the call cost."""


@dataclass(frozen=True)
class TutorResult:
    proposal: Proposal
    usage: Usage
    model: str  # the model that answered; a refused call may be served by a fallback


def diagnose(
    client: ModelClient, request: TutorRequest, max_tokens: int | None = None
) -> TutorResult:
    """One tutor call: the runs in, exactly one validated proposal out."""
    if not 1 <= len(request.runs) <= MAX_RUNS:
        raise ValueError(f"the tutor takes 1 to {MAX_RUNS} runs, got {len(request.runs)}")
    messages = [
        Message(role="system", content=render_system(request)),
        Message(role="user", content=render_runs(request)),
    ]
    reply = client.chat(messages, [PROPOSE_TOOL], GenParams(max_tokens=max_tokens))
    names = [c.name for c in reply.tool_calls]
    if names != [TOOL_NAME] or reply.malformed:
        # a call the provider could not parse is still an attempted call
        attempted = names + ["<malformed>"] * len(reply.malformed)
        raise TutorError(f"expected exactly one {TOOL_NAME} call, got {attempted}", reply.usage)
    try:
        proposal = Proposal.model_validate(
            {
                **reply.tool_calls[0].arguments,
                "schema_version": PROPOSAL_SCHEMA_VERSION,
                "proposal_id": str(ULID()),
                "source": "tutor",
            }
        )
    except ValidationError as exc:
        raise TutorError(f"the proposal is malformed: {exc}", reply.usage) from exc
    # validated first, so the IDs are well-formed strings before they are compared
    unknown = sorted(set(proposal.evidence_run_ids) - {d.run_id for d in request.runs})
    if unknown:
        raise TutorError(f"the proposal cites runs it was not shown: {unknown}", reply.usage)
    served = getattr(reply.raw, "model", None) or client.info().model
    return TutorResult(proposal=proposal, usage=reply.usage, model=served)
