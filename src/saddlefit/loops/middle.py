"""The middle loop: diagnose a training batch, try candidates, keep what passes both gates (A7).

One round:

1. run the current harness (``main``) on a batch of training tasks;
2. send up to six failed runs to the tutor, once per candidate, and validate each proposal;
3. run every candidate on the same batch and seeds; one that solves strictly more tasks
   (batch gate) is compared on the held-out tasks (held-out gate);
4. accept the best candidate that passes both (tie-break), reject the rest;
5. record every decided candidate in the library and let the tutor reflect on it.

The loop reaches the student only through ``Practice``. Held-out results come back as one
aggregate per comparison, so the loop never holds per-task held-out outcomes, and nothing
here can run control tasks (B10).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Final, Literal, Protocol

from saddlefit.config import TokenPrices
from saddlefit.eval.decisions import (
    Candidate,
    HeldOutComparison,
    batch_gate,
    held_out_gate,
    tie_break,
)
from saddlefit.eval.tamper import diff_paths
from saddlefit.harness.proposals import Proposal, ProposalError, ScriptLinter, apply_proposal
from saddlefit.harness.store import MAIN, HarnessStore, HarnessStoreError
from saddlefit.playbook import tutor_lines
from saddlefit.providers.base import ModelClient, ProviderError, Usage
from saddlefit.providers.cost import cost_usd
from saddlefit.storage.library import (
    CHANGE_SCHEMA_VERSION,
    ChangeRecord,
    HeldOutCounts,
    TrainCounts,
    next_change_id,
    write_record,
)
from saddlefit.tutor.diagnose import MAX_RUNS, RunDigest, TutorRequest, diagnose
from saddlefit.tutor.reflect import Decision, record_lesson, reflect


@dataclass(frozen=True)
class BatchResult:
    """Training tasks under one harness: solved, unsolved or inconclusive, and the failures."""

    solved: dict[str, bool | None]
    failures: tuple[RunDigest, ...] = ()


class Practice(Protocol):
    """The student, as the middle loop may use it."""

    @property
    def runs_per_task(self) -> int: ...

    def run_batch(self, harness_ref: str, task_ids: Sequence[str]) -> BatchResult:
        """Run training tasks with the harness at ``harness_ref``, same seeds every time."""
        ...

    def compare_held_out(self, current_ref: str, candidate_ref: str) -> HeldOutComparison:
        """Held-out wins and losses of the candidate against the current harness."""
        ...


@dataclass
class Budget:
    """Tutor calls and spend for one round (B4); reflection calls count too."""

    max_calls: int
    max_usd: float
    prices: TokenPrices
    calls: int = 0
    usd: float = 0.0
    # told of every charge as it happens, so spending survives a round that fails later
    on_charge: Callable[[float], None] | None = None

    def can_call(self) -> bool:
        return self.calls < self.max_calls and self.usd < self.max_usd

    def charge(self, usage: Usage | None) -> None:
        usd = 0.0 if usage is None else cost_usd(usage, self.prices)
        self.calls += 1
        self.usd += usd
        if self.on_charge is not None:
            self.on_charge(usd)


@dataclass(frozen=True)
class Tried:
    proposal: Proposal
    candidate_ref: str
    train_after: int
    held_out: HeldOutComparison | None


# why a tutor attempt produced no candidate: the call failed or its answer was unusable, or the
# proposal was refused (a disallowed file, a failed linter, a patch that does not apply)
REFUSED_BY_TUTOR: Final = "tutor"
REFUSED_PROPOSAL: Final = "proposal"


@dataclass
class RoundReport:
    batch: tuple[str, ...]
    train_before: int
    accepted: str | None = None  # proposal ID
    records: list[ChangeRecord] = field(default_factory=list)
    refused: int = 0  # tutor errors and proposals refused by validation
    refusals: list[str] = field(default_factory=list)  # "<kind>: <reason>", one per refusal
    lessons: int = 0
    pruned: list[str] = field(default_factory=list)  # proposal IDs ablation dropped after it
    tutor_calls: int = 0
    tutor_usd: float = 0.0


def _solved(result: BatchResult) -> int:
    return sum(v is True for v in result.solved.values())


def pick_batch(train_tasks: Sequence[str], round_index: int, size: int) -> tuple[str, ...]:
    """A deterministic window over the training tasks that moves on every round."""
    tasks = sorted(train_tasks)
    if not tasks:
        raise ValueError("there are no training tasks")
    size = min(size, len(tasks))
    start = (round_index * size) % len(tasks)
    return tuple(tasks[(start + i) % len(tasks)] for i in range(size))


@dataclass
class MiddleLoop:
    store: HarnessStore
    practice: Practice
    tutor: ModelClient
    linter: ScriptLinter
    root: Path  # .saddlefit/, where library/ lives
    train_tasks: Sequence[str]
    student_model: str
    student_digest: str | None
    context_budget: int
    max_calls_per_round: int
    max_usd_per_round: float
    prices: TokenPrices
    batch_size: int = 6
    candidates_per_round: int = 3
    today: Callable[[], date] = date.today
    # every tutor call this loop has paid for, failed rounds included
    spent_calls: int = 0
    spent_usd: float = 0.0
    # called after every paid tutor call, so its cost can be saved at once
    on_charge: Callable[[], None] | None = None

    def round(self, index: int) -> RoundReport:
        batch = pick_batch(self.train_tasks, index, self.batch_size)
        budget = Budget(
            self.max_calls_per_round, self.max_usd_per_round, self.prices, on_charge=self._charged
        )
        before = self.practice.run_batch(MAIN, batch)
        report = RoundReport(batch=batch, train_before=_solved(before))
        tried: list[Tried] = []
        try:
            if before.failures:
                self._try_candidates(batch, before, budget, report, tried)
            decided = self._decide(tried, report)
        except Exception:
            self._drop(tried)  # no candidate branch outlives a failed round
            raise
        # main moves only now: every candidate is decided before any lesson is written
        for t, status in decided:
            report.records.append(self._record(t, status, report.train_before, len(batch)))
        for t, status in decided:
            report.lessons += self._reflect(t, status, report.train_before, len(batch), budget)
        report.tutor_calls, report.tutor_usd = budget.calls, budget.usd
        return report

    def _charged(self, usd: float) -> None:
        self.spent_calls += 1
        self.spent_usd += usd
        if self.on_charge is not None:
            self.on_charge()

    def _decide(
        self, tried: list[Tried], report: RoundReport
    ) -> list[tuple[Tried, Literal["accepted", "rejected"]]]:
        """Accept the tie-break winner among candidates passing both gates; reject the rest."""
        passing = [
            Candidate(t.proposal.proposal_id, i, t.held_out, t.proposal.context_tokens_delta)
            for i, t in enumerate(tried)
            if t.held_out is not None and held_out_gate(t.held_out)
        ]
        winner = tie_break(passing)
        decided: list[tuple[Tried, Literal["accepted", "rejected"]]] = []
        for t in list(tried):
            if winner and winner.proposal_id == t.proposal.proposal_id:
                self.store.accept(t.proposal.proposal_id)
                report.accepted = t.proposal.proposal_id
                decided.append((t, "accepted"))
            else:
                self.store.reject(t.proposal.proposal_id)
                decided.append((t, "rejected"))
            tried.remove(t)  # decided, so no longer cleaned up on failure
        return decided

    def _drop(self, tried: list[Tried]) -> None:
        """Reject undecided candidates; a failing cleanup never hides the original error."""
        for t in tried:
            with contextlib.suppress(HarnessStoreError):
                self.store.reject(t.proposal.proposal_id)

    def _try_candidates(
        self,
        batch: tuple[str, ...],
        before: BatchResult,
        budget: Budget,
        report: RoundReport,
        tried: list[Tried],
    ) -> None:
        """Fill ``tried``; a candidate is in it from the moment its branch exists."""
        for _ in range(self.candidates_per_round):
            if not budget.can_call():
                break
            files = self.store.texts()
            request = TutorRequest(
                runs=before.failures[:MAX_RUNS],
                # lessons have their own section of the prompt
                harness_files={p: t for p, t in files.items() if p != "EXPERIENCE.md"},
                experience=files.get("EXPERIENCE.md", ""),
                context_budget=self.context_budget,
                playbook=tutor_lines(t for d in before.failures[:MAX_RUNS] for t in d.tags),
            )
            try:
                result = diagnose(self.tutor, request)
            except ProviderError as exc:  # TutorError included; the call was paid for
                budget.charge(exc.usage)
                report.refused += 1
                report.refusals.append(f"{REFUSED_BY_TUTOR}: {exc}")
                continue
            budget.charge(result.usage)
            try:
                ref = apply_proposal(self.store, result.proposal, self.linter)
            except ProposalError as exc:
                report.refused += 1
                report.refusals.append(f"{REFUSED_PROPOSAL}: {exc}")
                continue
            try:
                after = self.practice.run_batch(ref, batch)
                held_out = None
                if batch_gate(before.solved, after.solved):
                    held_out = self.practice.compare_held_out(MAIN, ref)
            except Exception:
                with contextlib.suppress(HarnessStoreError):
                    self.store.reject(result.proposal.proposal_id)
                raise
            tried.append(Tried(result.proposal, ref, _solved(after), held_out))

    def _record(
        self, t: Tried, status: Literal["accepted", "rejected"], before: int, tasks: int
    ) -> ChangeRecord:
        runs = self.practice.runs_per_task
        record = ChangeRecord(
            schema_version=CHANGE_SCHEMA_VERSION,
            id=next_change_id(self.root, self.today()),
            proposal_id=t.proposal.proposal_id,
            model=self.student_model,
            model_digest=self.student_digest,
            level=t.proposal.level,
            files=tuple(sorted(set(diff_paths(t.proposal.patch)))),
            harness_commit=t.candidate_ref,
            status=status,
            train=TrainCounts(before=before, after=t.train_after, tasks=tasks, runs_per_task=runs),
            held_out=(
                None
                if t.held_out is None
                else HeldOutCounts(
                    wins=t.held_out.wins,
                    losses=t.held_out.losses,
                    tasks=t.held_out.tasks,
                    runs_per_task=runs,
                )
            ),
            context_tokens_added=t.proposal.context_tokens_delta,
        )
        write_record(self.root, record)
        return record

    def _reflect(
        self,
        t: Tried,
        status: Literal["accepted", "rejected"],
        before: int,
        tasks: int,
        budget: Budget,
    ) -> int:
        """One optional lesson per decision; returns 1 if a lesson was recorded."""
        if not budget.can_call():
            return 0
        decision = Decision(t.proposal, status, before, t.train_after, tasks)
        try:
            experience = self.store.read("EXPERIENCE.md")
        except HarnessStoreError:
            return 0  # an accepted change may have removed the file; no lesson then
        try:
            result = reflect(self.tutor, decision, experience)
        except ProviderError as exc:
            budget.charge(exc.usage)
            return 0
        budget.charge(result.usage)
        if result.lesson is None:
            return 0
        try:
            recorded = record_lesson(self.store, result.lesson, self.student_model, self.linter)
        except (ProposalError, ProviderError, HarnessStoreError):
            return 0  # a lesson is optional; it never stops the round
        return 0 if recorded is None else 1
