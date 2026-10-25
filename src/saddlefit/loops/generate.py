"""``tasks generate``: generate, accept, calibrate and keep tasks of one kind (A11, B13).

Each attempt is one generator call. Its task goes to the sealed store, must fail without
its reference patch and pass with it, and is then run five times by the current student:
it is kept only when the student passes it in 20-80% of those runs (one to four). A task
that is not kept leaves nothing behind. The command stops at the wanted count, or before
a call that could pass the generator's call or dollar budget.
"""

from __future__ import annotations

import hashlib
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from saddlefit.config import GeneratorConfig
from saddlefit.providers.base import ProviderError
from saddlefit.providers.cost import PER_MTOK, cost_usd
from saddlefit.tasks.acceptance import Acceptance
from saddlefit.tasks.generator import GeneratedTask, GeneratorResult

CALIBRATION_RUNS: Final = 5
KEEP_PASSES: Final = range(1, 5)  # 20-80% of five runs
MAX_REPLY_TOKENS: Final = 8_192
_SLUG = re.compile(r"[^a-z0-9]+")


def task_id_for(kind: str, task: GeneratedTask) -> str:
    """``gen-<kind>-<title slug>-<12 hex>``, unique per task text."""
    title = task.task_md.splitlines()[0].removeprefix("# ").lower()
    slug = _SLUG.sub("-", title).strip("-")[:40].strip("-") or "task"
    digest = hashlib.sha256(task.task_md.encode("utf-8")).hexdigest()[:12]
    return f"gen-{kind}-{slug}-{digest}"


def worst_case_usd(prompt_chars: int, prices: GeneratorConfig) -> float:
    """An upper bound for one call: the prompt at about four characters a token, full reply."""
    p = prices.prices_usd_per_mtok
    return (prompt_chars / 4 * p.input + MAX_REPLY_TOKENS * p.output) / PER_MTOK


class NoCandidate(Exception):
    """This attempt found nothing to send to the model; it costs no call."""


class CalibrationError(Exception):
    """Calibration could not count five real runs, so the task cannot be judged."""


@dataclass(frozen=True)
class Attempt:
    task_id: str | None
    kept: bool
    reason: str
    passes: int | None = None


@dataclass
class GenerationReport:
    attempts: list[Attempt] = field(default_factory=list)
    calls: int = 0
    usd: float = 0.0
    stopped: str | None = None

    @property
    def kept(self) -> list[str]:
        return [a.task_id for a in self.attempts if a.kept and a.task_id]


@dataclass
class Generation:
    """One ``tasks generate`` command; every side effect is injected."""

    kind: str
    budget: GeneratorConfig
    # may raise NoCandidate; whatever it returns goes to prompt_chars and call
    request: Callable[[random.Random], Any]
    call: Callable[[Any], GeneratorResult]
    seal: Callable[[str, GeneratedTask], None]
    accept: Callable[[str, GeneratedTask], Acceptance]
    calibrate: Callable[[str, GeneratedTask], int]
    exists: Callable[[str], bool]
    keep: Callable[[str, GeneratedTask], None]
    discard: Callable[[str], None]
    prompt_chars: Callable[[Any], int]
    seed: int = 7
    max_attempts: int | None = None  # attempts of any kind, model calls or not

    def run(self, count: int) -> GenerationReport:
        report = GenerationReport()
        rng = random.Random(self.seed)  # noqa: S311 - which files to show, not cryptography
        while len(report.kept) < count:
            if report.calls >= self.budget.max_calls:
                report.stopped = f"generator.max_calls ({self.budget.max_calls}) reached"
                break
            if self.max_attempts is not None and len(report.attempts) >= self.max_attempts:
                report.stopped = f"{self.max_attempts} attempts made"
                break
            try:
                request = self.request(rng)
            except NoCandidate as exc:
                report.attempts.append(Attempt(None, False, str(exc)))
                continue
            if report.usd + worst_case_usd(self.prompt_chars(request), self.budget) > (
                self.budget.max_usd
            ):
                report.stopped = (
                    f"the next call could pass generator.max_usd ({self.budget.max_usd})"
                )
                break
            report.calls += 1
            report.attempts.append(self._attempt(request, report))
        return report

    def _attempt(self, request: Any, report: GenerationReport) -> Attempt:
        try:
            result = self.call(request)
        except ProviderError as exc:
            if exc.usage is not None:
                report.usd += cost_usd(exc.usage, self.budget.prices_usd_per_mtok)
            return Attempt(None, False, f"generator: {exc}")
        report.usd += cost_usd(result.usage, self.budget.prices_usd_per_mtok)
        task_id = task_id_for(self.kind, result.task)
        if self.exists(task_id):
            # sealing would overwrite the existing task, and discarding would delete it
            return Attempt(task_id, False, "the same task is already in the suite")
        try:
            self.seal(task_id, result.task)
            acceptance = self.accept(task_id, result.task)
            if not acceptance.accepted:
                self.discard(task_id)
                return Attempt(task_id, False, acceptance.reason)
            passes = self.calibrate(task_id, result.task)
        except CalibrationError as exc:
            self.discard(task_id)
            return Attempt(task_id, False, str(exc))
        except BaseException:
            self.discard(task_id)
            raise
        if passes not in KEEP_PASSES:
            self.discard(task_id)
            return Attempt(task_id, False, f"the student passed {passes} of 5", passes)
        self.keep(task_id, result.task)
        return Attempt(task_id, True, f"the student passed {passes} of 5", passes)
