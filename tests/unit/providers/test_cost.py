import pytest

from saddlefit.config import TokenPrices
from saddlefit.providers.base import Usage
from saddlefit.providers.cost import cost_usd

OPUS = TokenPrices(input=4, output=20, cache_write_5m=5, cache_write_1h=8, cache_read=0.2)


@pytest.mark.parametrize(
    ("usage", "usd"),
    [
        (Usage(), 0.0),
        (Usage(prompt_tokens=1_000_000), 4.0),
        (Usage(completion_tokens=1_000_000), 20.0),
        (Usage(cache_write_5m_tokens=1_000_000), 5.0),
        (Usage(cache_write_1h_tokens=1_000_000), 8.0),
        (Usage(cache_read_tokens=1_000_000), 0.2),
        # a cached tutor call: 40k harness read from cache, 2k fresh input, 2k output
        (Usage(prompt_tokens=2_000, completion_tokens=2_000, cache_read_tokens=40_000), 0.056),
    ],
)
def test_each_kind_of_token_has_its_price(usage: Usage, usd: float) -> None:
    assert cost_usd(usage, OPUS) == pytest.approx(usd)


def test_unset_prices_cost_nothing() -> None:
    assert cost_usd(Usage(prompt_tokens=10**6, cache_read_tokens=10**6), TokenPrices()) == 0.0
