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

import posixpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import PurePosixPath
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
from saddlefit.textdiff import file_diff

MAX_RUNS: Final = 6
LAST_EVENTS: Final = 30
CHECK_CHARS: Final = 2_000
LEVEL_ORDER: Final = ("mechanics", "tools", "text", "reasoning")
TOOL_NAME: Final = "propose"

PROPOSE_TOOL: Final = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Submit exactly one change to the harness as `edits`: each replaces `old`, text that "
        "occurs exactly once in a harness file, with `new`; an empty `old` creates a new file."
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
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old": {"type": "string"},
                        "new": {"type": "string"},
                    },
                    "required": ["path", "old", "new"],
                    "additionalProperties": False,
                },
            },
            "expected_effect": {"type": "string"},
            "context_tokens_delta": {"type": "integer"},
        },
        "required": [
            "level",
            "failure_tags",
            "evidence_run_ids",
            "technique",
            "rationale",
            "edits",
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


class EditsError(TutorError):
    """The proposal's edits do not apply to the harness: refused like a patch that does not."""


@dataclass(frozen=True)
class TutorResult:
    proposal: Proposal
    usage: Usage
    model: str  # the model that answered; a refused call may be served by a fallback


def patch_from_edits(edits: Any, files: Mapping[str, str]) -> str:
    """The diff the tutor's edits make to the harness files it was shown.

    The tutor writes no diff itself: hand-written hunks often failed to apply (#175). Each edit
    replaces ``old``, which must occur exactly once, with ``new``; an empty ``old`` creates a
    file that does not exist yet. Edits apply in order, so two may touch one file.
    """
    if not isinstance(edits, list) or not edits:
        raise ValueError("edits must list at least one edit")
    after: dict[str, str] = {}
    created: set[str] = set()
    for edit in edits:
        if not isinstance(edit, dict) or not all(
            isinstance(edit.get(k), str) for k in ("path", "old", "new")
        ):
            raise ValueError("each edit needs path, old and new strings")
        path, old, new = edit["path"], edit["old"], edit["new"]
        rel = PurePosixPath(path)
        if (
            "\\" in path
            or rel.is_absolute()
            or ".." in rel.parts
            or posixpath.normpath(path) != path
        ):
            raise ValueError(f"edit path must be relative and normal: {path!r}")
        current = after.get(path, files.get(path))
        if not old:
            if current is not None:
                raise ValueError(f"an edit with empty old creates a new file; {path} exists")
            after[path] = new
            created.add(path)
            continue
        if current is None:
            raise ValueError(f"an edit for a file that is not in the harness: {path}")
        first = current.find(old)
        if first < 0:
            raise ValueError(f"the old text of an edit occurs 0 times in {path}, not once")
        # a second match may overlap the first ("aa" in "aaa"), which str.count would miss
        if current.find(old, first + 1) >= 0:
            raise ValueError(f"the old text of an edit occurs more than once in {path}")
        after[path] = current.replace(old, new, 1)
    patch = "".join(
        file_diff(p, None if p in created else files[p], text) for p, text in sorted(after.items())
    )
    if not patch:
        raise ValueError("the edits change nothing")
    return patch


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
    arguments = dict(reply.tool_calls[0].arguments)
    # the edits apply to the files shown in the prompt, lessons included
    shown = dict(request.harness_files)
    if request.experience:
        shown["EXPERIENCE.md"] = request.experience
    try:
        patch = patch_from_edits(arguments.pop("edits", None), shown)
    except ValueError as exc:
        raise EditsError(f"the proposal's edits do not apply: {exc}", reply.usage) from exc
    try:
        proposal = Proposal.model_validate(
            {
                **arguments,
                "patch": patch,
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
