"""What an extraction run cost.

Token counts alone tell nobody anything. A dollar figure, shown next to the document it belongs
to, is what makes the cost of reading a contract a thing people can reason about — and it is
what makes a prompt cache that quietly stopped working visible instead of merely expensive.

Prices move. An unknown model returns `None` rather than a guess, and the interface shows a dash:
a confidently wrong number here is worse than no number, because nobody would think to check it.
"""

from __future__ import annotations

from dataclasses import dataclass

# US dollars per million tokens. Checked 2026-09-19 against Anthropic's published list prices.
# Cache writes bill at 1.25x the input rate and cache reads at 0.1x, which is where the saving
# in the five-call design comes from.
_CACHE_WRITE_MULTIPLIER = 1.25
_CACHE_READ_MULTIPLIER = 0.10


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float

    @property
    def cache_write_per_mtok(self) -> float:
        return self.input_per_mtok * _CACHE_WRITE_MULTIPLIER

    @property
    def cache_read_per_mtok(self) -> float:
        return self.input_per_mtok * _CACHE_READ_MULTIPLIER


# Keyed on a normalised model family. Bedrock inference profiles carry region prefixes and
# version suffixes ("global.anthropic.claude-sonnet-4-6", "apac.anthropic.claude-haiku-4-5-
# 20251001-v1:0"), so lookup matches on the family name appearing anywhere in the id.
_PRICES: dict[str, ModelPrice] = {
    "claude-opus-4": ModelPrice(15.00, 75.00),
    "claude-opus-5": ModelPrice(15.00, 75.00),
    "claude-sonnet-4": ModelPrice(3.00, 15.00),
    "claude-sonnet-5": ModelPrice(3.00, 15.00),
    "claude-haiku-4": ModelPrice(1.00, 5.00),
    "claude-3-5-haiku": ModelPrice(0.80, 4.00),
}


def price_for(model: str | None) -> ModelPrice | None:
    if not model:
        return None
    needle = model.lower()
    # Longest family name first, so "claude-sonnet-4-6" is not matched by a shorter prefix that
    # happens to also appear.
    for family in sorted(_PRICES, key=len, reverse=True):
        if family in needle:
            return _PRICES[family]
    return None


def cost_usd(
    model: str | None,
    *,
    input_tokens: int | None = 0,
    output_tokens: int | None = 0,
    cache_read_tokens: int | None = 0,
    cache_write_tokens: int | None = 0,
) -> float | None:
    """Cost of one call, or None when the model's price is unknown.

    Providers report cached tokens separately from `input_tokens`, so the three input figures
    are additive rather than overlapping.
    """
    price = price_for(model)
    if price is None:
        return None
    total = (
        (input_tokens or 0) * price.input_per_mtok
        + (output_tokens or 0) * price.output_per_mtok
        + (cache_read_tokens or 0) * price.cache_read_per_mtok
        + (cache_write_tokens or 0) * price.cache_write_per_mtok
    ) / 1_000_000
    return round(total, 6)


def uncached_cost_usd(
    model: str | None,
    *,
    input_tokens: int | None = 0,
    output_tokens: int | None = 0,
    cache_read_tokens: int | None = 0,
    cache_write_tokens: int | None = 0,
) -> float | None:
    """What the same call would have cost with no cache, for the saving figure."""
    price = price_for(model)
    if price is None:
        return None
    billed_input = (input_tokens or 0) + (cache_read_tokens or 0) + (cache_write_tokens or 0)
    total = (
        billed_input * price.input_per_mtok + (output_tokens or 0) * price.output_per_mtok
    ) / 1_000_000
    return round(total, 6)
