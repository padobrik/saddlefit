"""The price of a model call, from its usage and the user's price list (B4)."""

from __future__ import annotations

from saddlefit.config import TokenPrices
from saddlefit.providers.base import Usage

PER_MTOK = 1_000_000


def cost_usd(usage: Usage, prices: TokenPrices) -> float:
    """USD for one call; every kind of token at its own price."""
    return (
        usage.prompt_tokens * prices.input
        + usage.completion_tokens * prices.output
        + usage.cache_write_5m_tokens * prices.cache_write_5m
        + usage.cache_write_1h_tokens * prices.cache_write_1h
        + usage.cache_read_tokens * prices.cache_read
    ) / PER_MTOK
