"""Statistics for reports: Wilson intervals, paired bootstrap, delegation map (B12, B15).

The paired bootstrap works on per-task outcomes, so it lives here, in the evaluator's
package: callers outside it get only the interval.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from typing import Final, Literal

Z95: Final = 1.959963984540054
BOOTSTRAP_RESAMPLES: Final = 10_000
BOOTSTRAP_SEED: Final = 20261021
# B12 delegation map
LOCAL_SUCCESS: Final = 0.8
LOCAL_WILSON_LOW: Final = 0.5
BORDERLINE_SUCCESS: Final = 0.5
MIN_CONTROL_TASKS: Final = 5

Delegation = Literal["local", "borderline", "cloud", "insufficient data"]


def wilson(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    """The Wilson score interval for a binomial proportion (95% by default)."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def paired_bootstrap(
    before: Sequence[bool],
    after: Sequence[bool],
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float]:
    """95% interval for the success-rate difference ``after - before`` over paired tasks.

    Tasks are resampled with replacement, keeping each task's two outcomes together; the
    seed is fixed, so the same data always gives the same interval.
    """
    if len(before) != len(after):
        raise ValueError("paired outcomes need the same tasks before and after")
    n = len(before)
    if n == 0:
        return (0.0, 0.0)
    diffs = [int(a) - int(b) for a, b in zip(after, before, strict=True)]
    rng = random.Random(seed)  # noqa: S311 - resampling, not cryptography
    means = sorted(sum(rng.choices(diffs, k=n)) / n for _ in range(resamples))
    low = means[math.floor(0.025 * (resamples - 1))]
    high = means[math.ceil(0.975 * (resamples - 1))]
    return (low, high)


EXACT_FLIPS_UP_TO: Final = 16  # tasks with a difference; 2**16 sign patterns at most
FLIP_DRAWS: Final = 20_000


def paired_sign_flip_p(diffs: Sequence[int], seed: int = 0) -> float:
    """One-sided p-value that the differences are not above zero (B12).

    ``diffs`` are per-task differences in passing runs, candidate minus current, on the same
    seeds. Under no effect each difference is as likely to have either sign, so the p-value
    is the share of sign patterns whose sum is at least the observed one: exact for up to
    ``EXACT_FLIPS_UP_TO`` nonzero differences, else from a seeded sample.
    """
    nonzero = [abs(d) for d in diffs if d]
    observed = sum(diffs)
    if not nonzero or observed <= 0:
        return 1.0
    if len(nonzero) <= EXACT_FLIPS_UP_TO:
        sums = [0]
        for d in nonzero:  # every sum of +d or -d, by dynamic programming over the tasks
            sums = [s + d for s in sums] + [s - d for s in sums]
        return sum(s >= observed for s in sums) / len(sums)
    rng = random.Random(seed)  # noqa: S311 - a seeded resample, not cryptography
    hits = sum(
        sum(d if rng.random() < 0.5 else -d for d in nonzero) >= observed for _ in range(FLIP_DRAWS)
    )
    return (hits + 1) / (FLIP_DRAWS + 1)  # never 0 from a sample


def delegation(outcomes: Sequence[bool]) -> Delegation:
    """Where a task type belongs, from its control outcomes (B12)."""
    n = len(outcomes)
    if n < MIN_CONTROL_TASKS:
        return "insufficient data"
    solved = sum(outcomes)
    success = solved / n
    if success >= LOCAL_SUCCESS and wilson(solved, n)[0] >= LOCAL_WILSON_LOW:
        return "local"
    if success >= BORDERLINE_SUCCESS:
        return "borderline"
    return "cloud"


def delegation_map(by_type: Mapping[str, Sequence[bool]]) -> dict[str, Delegation]:
    return {task_type: delegation(outcomes) for task_type, outcomes in sorted(by_type.items())}


def context_efficiency(success_rate: float, harness_tokens: float) -> float:
    """Control success per 1,000 harness tokens (B15)."""
    if harness_tokens <= 0:
        raise ValueError("harness tokens must be positive")
    return success_rate / (harness_tokens / 1000)
