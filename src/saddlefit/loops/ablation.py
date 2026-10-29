"""Ablation: drop accepted changes that no longer earn their place (B12).

After every 3 accepted changes and after any change of the student model, each accepted
change is tested in reverse order by removing it: the harness without it runs against the
current one on every training task and on the held-out tasks. If removal gives
``wins - losses >= 0`` over both, the change is reverted on ``main`` for good and recorded
as ``pruned``. Held-out results arrive as aggregates only, as in a round (B10).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol

from saddlefit.eval.decisions import HeldOutComparison, ablation_prunes, compare_held_out
from saddlefit.harness.store import MAIN, HarnessStore, HarnessStoreError
from saddlefit.loops.middle import BatchResult, Practice
from saddlefit.storage.library import (
    CHANGE_SCHEMA_VERSION,
    ChangeRecord,
    HeldOutCounts,
    TrainCounts,
    next_change_id,
    read_records,
    write_record,
)


class Ablatable(Protocol):
    """What ablation needs; a ``MiddleLoop`` has it, and ``prune`` builds it without a tutor."""

    @property
    def store(self) -> HarnessStore: ...
    @property
    def practice(self) -> Practice: ...
    @property
    def root(self) -> Path: ...
    @property
    def train_tasks(self) -> Sequence[str]: ...
    @property
    def student_model(self) -> str: ...
    @property
    def student_digest(self) -> str | None: ...
    @property
    def today(self) -> Callable[[], date]: ...


@dataclass
class AblationTarget:
    """The project as ablation sees it, for ``prune``: no tutor, no API key."""

    store: HarnessStore
    practice: Practice
    root: Path
    train_tasks: Sequence[str]
    student_model: str
    student_digest: str | None
    today: Callable[[], date] = field(default=date.today)


@dataclass(frozen=True)
class AblationResult:
    proposal_id: str
    pruned: bool
    train: HeldOutComparison | None = None  # without the change, against the current harness
    held_out: HeldOutComparison | None = None
    skipped: str | None = None  # why the change could not be tested


def active_changes(records: Sequence[ChangeRecord]) -> list[ChangeRecord]:
    """Accepted changes not pruned since, oldest first."""
    pruned = {r.proposal_id for r in records if r.status == "pruned"}
    return [r for r in records if r.status == "accepted" and r.proposal_id not in pruned]


def _solved(result: BatchResult) -> int:
    return sum(v is True for v in result.solved.values())


def ablate(loop: Ablatable) -> list[AblationResult]:
    """Test every active change, newest first, and prune those that do not help."""
    train = sorted(loop.train_tasks)
    results = []
    current: tuple[str, BatchResult] | None = None  # main's training results, per commit
    for record in reversed(active_changes(read_records(loop.root))):
        merge = loop.store.accept_commit(record.proposal_id)
        try:
            ref = loop.store.without(merge, record.proposal_id)
        except HarnessStoreError as exc:
            # a later change built on this one; it stays until that one goes
            results.append(AblationResult(record.proposal_id, False, skipped=str(exc)))
            continue
        try:
            head = loop.store.head()
            if current is None or current[0] != head:
                current = (head, loop.practice.run_batch(MAIN, train))
            without = loop.practice.run_batch(ref, train)
            train_cmp = compare_held_out(
                current[1].solved, without.solved, current[1].passes, without.passes
            )
            held_out = loop.practice.compare_held_out(MAIN, ref)
        finally:
            loop.store.drop_ablation(record.proposal_id)
        pruned = ablation_prunes(train_cmp, held_out)
        if pruned:
            commit = loop.store.prune(
                merge,
                f"ablation: without it, training passes {train_cmp.net_passes:+d}, "
                f"held-out passes {held_out.net_passes:+d}; not clearly worse",
            )
            write_record(
                loop.root,
                _pruned_record(
                    loop,
                    accepted=record,
                    commit=commit,
                    with_change=current[1],
                    without=without,
                    held_out=held_out,
                    tasks=len(train),
                ),
            )
        results.append(AblationResult(record.proposal_id, pruned, train_cmp, held_out))
    return results


def _pruned_record(
    loop: Ablatable,
    *,
    accepted: ChangeRecord,
    commit: str,
    with_change: BatchResult,
    without: BatchResult,
    held_out: HeldOutComparison,
    tasks: int,
) -> ChangeRecord:
    runs = loop.practice.runs_per_task
    return ChangeRecord(
        schema_version=CHANGE_SCHEMA_VERSION,
        id=next_change_id(loop.root, loop.today()),
        proposal_id=accepted.proposal_id,
        model=loop.student_model,
        model_digest=loop.student_digest,
        level=accepted.level,
        files=accepted.files,
        harness_commit=commit,
        status="pruned",
        # before: with the change; after: without it
        train=TrainCounts(
            before=_solved(with_change), after=_solved(without), tasks=tasks, runs_per_task=runs
        ),
        held_out=HeldOutCounts(
            wins=held_out.wins, losses=held_out.losses, tasks=held_out.tasks, runs_per_task=runs
        ),
        context_tokens_added=-accepted.context_tokens_added,
    )
