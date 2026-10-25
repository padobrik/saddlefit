"""The starter playbook: known small-model mechanics, before any statistics exist (A6).

Each entry is a change to ``loop.yaml``, kept as the values to set rather than as a patch,
so it applies to whatever the harness looks like when it is used: ``proposal_for`` diffs
the current ``loop.yaml`` against the same file with the entry merged in. The fast loop
probes these entries, and the tutor sees the ones that match its runs' failure tags.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping, Sequence
from functools import cache
from importlib import resources
from typing import Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from ulid import ULID

from saddlefit.eval.taxonomy import Tag
from saddlefit.harness.loop_config import LoopConfig, dump_loop_config
from saddlefit.harness.proposals import PROPOSAL_SCHEMA_VERSION, Proposal

PLAYBOOK_SCHEMA_VERSION: Final = 1


class PlaybookEntry(BaseModel):
    """One technique (B8), with the loop.yaml values that make it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9-]+$")
    technique: str
    failure_tags: tuple[Tag, ...] = Field(min_length=1)
    level: Literal["mechanics", "tools", "text", "reasoning"]
    rationale: str
    loop: dict[str, Any]
    proposed: int = 0
    accepted: int = 0

    @property
    def acceptance_rate(self) -> float:
        return self.accepted / self.proposed if self.proposed else 0.0


@cache
def starter_playbook() -> tuple[PlaybookEntry, ...]:
    text = (resources.files("saddlefit.starter_playbook") / "entries.yaml").read_text("utf-8")
    return tuple(PlaybookEntry.model_validate(raw) for raw in yaml.safe_load(text))


def for_tags(
    tags: Iterable[str], entries: Sequence[PlaybookEntry] | None = None
) -> list[PlaybookEntry]:
    """Entries for any of ``tags``, the most-matching first, then by acceptance, then order."""
    wanted = set(tags)
    pool = list(starter_playbook() if entries is None else entries)
    scored = [(len(wanted & set(e.failure_tags)), e) for e in pool]
    matching = [(n, i, e) for i, (n, e) in enumerate(scored) if n]
    matching.sort(key=lambda t: (-t[0], -t[2].acceptance_rate, t[1]))
    return [e for _, _, e in matching]


def _merge(base: dict[str, Any], change: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in change.items():
        if key == "hooks":
            merged[key] = [*merged.get(key, []), *value]
        elif isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def applied(loop: LoopConfig, entries: Iterable[PlaybookEntry]) -> LoopConfig:
    """``loop`` with every entry merged in, in order; validated like any loop.yaml."""
    raw = loop.model_dump(mode="json")
    for entry in entries:
        raw = _merge(raw, entry.loop)
    return LoopConfig.model_validate(raw)


def loop_patch(before: str, after: str) -> str:
    """A git-style diff of ``loop.yaml``, as the harness store applies it."""
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile="a/loop.yaml",
        tofile="b/loop.yaml",
    )
    body = "".join(lines)
    return f"diff --git a/loop.yaml b/loop.yaml\n{body}" if body else ""


def proposal_for(
    entries: Sequence[PlaybookEntry], loop_yaml: str, loop: LoopConfig, source: str = "playbook"
) -> Proposal | None:
    """One proposal that makes all of ``entries``; ``None`` when they change nothing."""
    patch = loop_patch(loop_yaml, dump_loop_config(applied(loop, entries)))
    if not patch:
        return None
    tags = sorted({t for e in entries for t in e.failure_tags})
    return Proposal.model_validate(
        {
            "schema_version": PROPOSAL_SCHEMA_VERSION,
            "proposal_id": str(ULID()),
            "source": source,
            "level": entries[0].level,
            "failure_tags": tags,
            "evidence_run_ids": [],
            "technique": " + ".join(e.technique for e in entries),
            "rationale": " ".join(e.rationale for e in entries),
            "patch": patch,
            "expected_effect": f"Fewer {', '.join(tags)} failures",
            "context_tokens_delta": 0,
        }
    )


def tutor_lines(tags: Iterable[str], limit: int = 5) -> list[str]:
    """The playbook section of the tutor's prompt: techniques for these failure tags."""
    lines = []
    for e in for_tags(tags)[:limit]:
        change = yaml.safe_dump(e.loop, default_flow_style=True, width=200).strip()
        lines.append(f"{e.technique} ({', '.join(e.failure_tags)}; loop.yaml {change})")
    return lines
