"""Retries with exponential backoff and jitter, for transient provider errors only (B5)."""

from __future__ import annotations

import random
import time
from collections.abc import Callable

from saddlefit.providers.base import TransientProviderError

MAX_RETRIES = 3
LOCAL_TIMEOUT_S = 120.0
API_TIMEOUT_S = 300.0


def call_with_retries[T](
    fn: Callable[[], T],
    *,
    retries: int = MAX_RETRIES,
    base_delay_s: float = 0.5,
    max_delay_s: float = 8.0,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> T:
    """Call ``fn``; on ``TransientProviderError`` retry up to ``retries`` times.

    Any other exception propagates at once. Tool executions must never go through here.
    """
    jitter = rng or random.Random()  # noqa: S311 - jitter, not cryptography
    attempt = 0
    while True:
        try:
            return fn()
        except TransientProviderError:
            if attempt >= retries:
                raise
            delay = min(max_delay_s, base_delay_s * 2**attempt)
            sleep(delay * (0.5 + jitter.random() / 2))
            attempt += 1
