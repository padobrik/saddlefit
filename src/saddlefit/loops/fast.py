"""The fast loop: rescue one task by probing playbook variants of the harness (A6, B12).

Attempt with the current harness. If the task's own check fails, propose up to
``max_variants`` playbook entries for the attempt's failure tags, probe each in
``runs_per_variant`` runs, compose the ones that helped and run the check once more. A
variant counts only if it passes every run when ``runs_per_variant`` is at most 2, and a
majority otherwise. Variants exist only as loop settings handed to ``attempt``: nothing
here can reach the harness store, so a change found here stays on probation (required
test 7) and goes to the journal with its evidence.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from saddlefit.config import FastLoopConfig
from saddlefit.harness.loop_config import LoopConfig
from saddlefit.playbook import PlaybookEntry, applied, for_tags, starter_playbook


@dataclass(frozen=True)
class Probe:
    """One run of the student on the task: the check's verdict and what it changed."""

    run_id: str
    passed: bool
    tags: tuple[str, ...]
    diff: str


@dataclass
class FastResult:
    passed: bool
    diff: str  # the answer: the passing attempt's diff, or the best one when none passed
    probes: list[Probe] = field(default_factory=list)
    helped: list[PlaybookEntry] = field(default_factory=list)  # the composed variants
    stopped: str | None = None  # why probing stopped early, if it did

    @property
    def rescued(self) -> bool:
        """Passed only thanks to playbook variants, so a journal entry is due."""
        return self.passed and bool(self.helped)


def variant_passes(passes: Sequence[bool], runs_per_variant: int) -> bool:
    """B12: every run when there are at most two, otherwise a majority."""
    if runs_per_variant <= 2:
        return len(passes) == runs_per_variant and all(passes)
    return sum(passes) * 2 > runs_per_variant


@dataclass
class FastLoop:
    budget: FastLoopConfig
    loop: LoopConfig  # the harness's own, unchanged by anything here
    attempt: Callable[[LoopConfig, int], Probe]  # loop settings and a seed
    seed: int = 7
    clock: Callable[[], float] = time.monotonic
    playbook: Sequence[PlaybookEntry] = field(default_factory=starter_playbook)

    def run(self) -> FastResult:
        started = self.clock()
        deadline = started + self.budget.time_budget_minutes * 60
        first = self.attempt(self.loop, self.seed)
        result = FastResult(passed=first.passed, diff=first.diff, probes=[first])
        if first.passed:
            return result
        best = first
        helped: list[PlaybookEntry] = []
        candidates = [
            e for e in for_tags(first.tags, self.playbook) if applied(self.loop, [e]) != self.loop
        ][: self.budget.max_variants]
        for index, entry in enumerate(candidates):
            passes = []
            for run in range(self.budget.runs_per_variant):
                if self.clock() >= deadline:
                    result.stopped = "time budget"
                    break
                probe = self.attempt(applied(self.loop, [entry]), self.seed + 1 + index * 10 + run)
                result.probes.append(probe)
                passes.append(probe.passed)
                if probe.passed and not best.passed:
                    best = probe
            if variant_passes(passes, self.budget.runs_per_variant):
                helped.append(entry)
            if result.stopped:
                break
        result.diff = best.diff
        if not helped or self.clock() >= deadline:
            result.stopped = result.stopped or (
                "no variant helped" if not helped else "time budget"
            )
            return result
        final = self.attempt(applied(self.loop, helped), self.seed)
        result.probes.append(final)
        result.helped = helped
        result.passed = final.passed
        result.diff = final.diff if final.passed else best.diff
        return result
