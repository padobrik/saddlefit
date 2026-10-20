import random

import pytest

from saddlefit.providers.base import ProviderError, TransientProviderError
from saddlefit.providers.retry import call_with_retries


class Flaky:
    def __init__(self, failures: int, error: type[Exception] = TransientProviderError) -> None:
        self.failures = failures
        self.error = error
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error("boom")
        return "ok"


def run(fn: Flaky, delays: list[float]) -> str:
    return call_with_retries(fn, sleep=delays.append, rng=random.Random(0))


def test_recovers_from_transient_errors_with_backoff() -> None:
    delays: list[float] = []
    fn = Flaky(3)
    assert run(fn, delays) == "ok"
    assert fn.calls == 4
    assert len(delays) == 3
    # exponential with jitter in [0.5, 1.0) of the nominal delay
    for nominal, actual in zip([0.5, 1.0, 2.0], delays, strict=True):
        assert nominal * 0.5 <= actual < nominal


def test_gives_up_after_three_retries() -> None:
    fn = Flaky(10)
    with pytest.raises(TransientProviderError):
        run(fn, [])
    assert fn.calls == 4


def test_permanent_errors_are_not_retried() -> None:
    fn = Flaky(1, ProviderError)
    with pytest.raises(ProviderError):
        run(fn, [])
    assert fn.calls == 1
