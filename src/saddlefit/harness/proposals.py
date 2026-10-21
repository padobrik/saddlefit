"""Proposals and their validation before anything reaches the harness (B8, hard rule 3).

A proposal becomes a candidate commit only through ``apply_proposal``: the store refuses a
patch that does not apply cleanly, touches a path outside the allowlist, changes more than
200 lines or breaks ``loop.yaml``; then every script it adds or changes must pass the
linter, which runs inside the sandbox. A refused proposal leaves no candidate behind.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Annotated, Final, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from saddlefit.eval.taxonomy import Tag
from saddlefit.harness.store import HarnessStore, HarnessStoreError

PROPOSAL_SCHEMA_VERSION: Final = 1
# Crockford base32, as python-ulid writes it; a first digit above 7 would overflow 128 bits
ULID_PATTERN: Final = r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"
# B8: tool scripts are Python, checked by ruff, or shell, checked by shellcheck
SCRIPT_SUFFIXES: Final = frozenset({".py", ".sh"})

Ulid = Annotated[str, StringConstraints(pattern=ULID_PATTERN)]


class Proposal(BaseModel):
    """One change to the harness, from the tutor, the fast loop or the playbook (B8)."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal[1]
    proposal_id: Ulid
    source: Literal["tutor", "fast_loop", "playbook"]
    level: Literal["mechanics", "tools", "text", "reasoning"]
    failure_tags: tuple[Tag, ...]
    evidence_run_ids: tuple[Ulid, ...]  # run IDs are ULIDs too
    technique: str = Field(min_length=1)
    rationale: str
    patch: str = Field(min_length=1)
    expected_effect: str
    context_tokens_delta: int

    @model_validator(mode="after")
    def _tutor_cites_evidence(self) -> Self:
        """B13: a tutor proposal MUST cite the runs it is based on."""
        if self.source == "tutor" and not self.evidence_run_ids:
            raise ValueError("a tutor proposal must cite at least one evidence run ID")
        return self


class ProposalError(Exception):
    """The proposal was refused; the harness is unchanged."""


class ScriptLinter(Protocol):
    def lint(self, scripts: Mapping[str, bytes]) -> list[str]:
        """Problems found in the scripts (path -> content); empty when they all pass."""
        ...


def scripts_to_lint(files: Mapping[str, bytes]) -> dict[str, bytes]:
    """The scripts among changed files; ``tools/`` may hold nothing but scripts."""
    others = sorted(
        p
        for p in files
        if p.startswith("tools/") and PurePosixPath(p).suffix not in SCRIPT_SUFFIXES
    )
    if others:
        raise ProposalError(f"tools/ holds only .py and .sh scripts, not: {others}")
    return {p: data for p, data in files.items() if PurePosixPath(p).suffix in SCRIPT_SUFFIXES}


def apply_proposal(store: HarnessStore, proposal: Proposal, linter: ScriptLinter) -> str:
    """Validate ``proposal`` and commit it as a candidate; return the candidate SHA."""
    try:
        sha = store.propose(proposal.proposal_id, proposal.patch, proposal.technique)
    except HarnessStoreError as exc:
        raise ProposalError(str(exc)) from exc
    try:
        scripts = scripts_to_lint(store.candidate_files(proposal.proposal_id))
        problems = linter.lint(scripts) if scripts else []
        if problems:
            raise ProposalError("scripts fail the linter:\n" + "\n".join(problems))
    except BaseException:
        store.reject(proposal.proposal_id)
        raise
    return sha
