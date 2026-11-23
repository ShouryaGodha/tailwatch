"""List prices, USD per million tokens: (input, cached input read, output).

Cost is real money, so an unknown model is still called but its reply is marked unpriced and
the run log says so. Add or override prices in ``tailwatch.yaml`` under ``pricing:``.
Anthropic cache writes cost 1.25x the input price.
"""

from __future__ import annotations

from collections.abc import Mapping

PRICES: dict[str, tuple[float, float, float]] = {
    # Anthropic, first-party list prices.
    "claude-fable-5-2": (8.00, 0.20, 40.00),
    "claude-fable-5-1": (10.00, 1.00, 50.00),
    "claude-opus-5-5": (4.00, 0.20, 20.00),
    "claude-opus-5": (5.00, 0.50, 25.00),
    "claude-opus-4-8": (5.00, 0.50, 25.00),
    "claude-sonnet-5-5": (2.00, 0.10, 10.00),
    "claude-sonnet-5": (2.00, 0.20, 10.00),
    "claude-sonnet-4-6": (3.00, 0.30, 15.00),
    "claude-haiku-5-5": (0.10, 0.01, 0.50),
    "claude-haiku-4-5": (1.00, 0.10, 5.00),
    # Local models cost nothing per token.
    "ollama:*": (0.0, 0.0, 0.0),
}
CACHE_WRITE_MULTIPLIER = 1.25


def price_for(provider: str, model: str, extra: Mapping[str, tuple[float, float, float]] | None = None
              ) -> tuple[float, float, float] | None:
    table = {**PRICES, **(extra or {})}
    for key in (f"{provider}:{model}", model, f"{provider}:*"):
        if key in table:
            return table[key]
    return None


def cost_of(provider: str, model: str, *, tokens_in: int, tokens_cached: int, tokens_cache_write: int,
            tokens_out: int, extra: Mapping[str, tuple[float, float, float]] | None = None) -> tuple[float, bool]:
    """(USD, priced). ``tokens_in`` counts uncached input only."""
    price = price_for(provider, model, extra)
    if price is None:
        return 0.0, False
    p_in, p_cached, p_out = price
    usd = (tokens_in * p_in + tokens_cached * p_cached + tokens_cache_write * p_in * CACHE_WRITE_MULTIPLIER
           + tokens_out * p_out) / 1_000_000
    return round(usd, 6), True
