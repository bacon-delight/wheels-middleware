"""What an extraction run cost.

Token counts alone tell nobody anything. A dollar figure, shown next to the document it belongs
to, is what makes the cost of reading a contract a thing people can reason about — and it is
what makes a prompt cache that quietly stopped working visible instead of merely expensive.

Prices move. An unknown model returns `None` rather than a guess, and the interface shows a dash:
a confidently wrong number here is worse than no number, because nobody would think to check it.
"""

from __future__ import annotations

from dataclasses import dataclass

# US dollars per million tokens, read from the AWS Price List API on 2026-09-19 for the regions
# this account calls (ap-south-2 for Anthropic via global.*, ap-south-1 for Amazon Nova).
#
# Anthropic bills a cache write at 1.25x input and a cache read at 0.1x. Those ratios are NOT
# universal: Nova writes to cache for nothing and reads at 0.25x. They were applied to every
# model here, so a model added without thinking would have been priced on Anthropic's economics.
# Each row now states its own cache rates, and the multipliers are only a default.
_CACHE_WRITE_MULTIPLIER = 1.25
_CACHE_READ_MULTIPLIER = 0.10


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float
    # None means "derive from the input rate the Anthropic way", which is right for Claude and
    # wrong for everything else.
    cache_write: float | None = None
    cache_read: float | None = None
    # The largest single response the model will produce. The extractor asks for up to 32,000
    # tokens in one call (service.CALL_BUDGET); asking a 5,000-token model for that is a
    # ValidationException, so the budget is clamped against this.
    max_output_tokens: int = 64000

    @property
    def cache_write_per_mtok(self) -> float:
        if self.cache_write is not None:
            return self.cache_write
        return self.input_per_mtok * _CACHE_WRITE_MULTIPLIER

    @property
    def cache_read_per_mtok(self) -> float:
        if self.cache_read is not None:
            return self.cache_read
        return self.input_per_mtok * _CACHE_READ_MULTIPLIER


# Keyed on a normalised model family. Bedrock inference profiles carry region prefixes and
# version suffixes ("global.anthropic.claude-sonnet-4-6", "apac.anthropic.claude-haiku-4-5-
# 20251001-v1:0"), so lookup matches on the family name appearing anywhere in the id.
_PRICES: dict[str, ModelPrice] = {
    # Opus was carrying the Claude 3 Opus rate, three generations retired, and Sonnet 5 was 50%
    # over — both of them rows the configuration names as its next step, so the decision to move
    # was being made against wrong numbers.
    "claude-opus-4": ModelPrice(5.00, 25.00),
    "claude-opus-5": ModelPrice(5.00, 25.00),
    "claude-sonnet-4": ModelPrice(3.00, 15.00),
    "claude-sonnet-5": ModelPrice(2.00, 10.00),
    "claude-haiku-4": ModelPrice(1.00, 5.00),
    "claude-3-5-haiku": ModelPrice(0.80, 4.00),
    # Amazon Nova, ap-south-1. Cache writes are free and reads are a quarter of input, not a
    # tenth. Micro, Lite and Pro stop at 5,000 output tokens, which is why a long category has
    # to be asked for in windows rather than all at once.
    "nova-2-lite": ModelPrice(0.35, 2.95, cache_write=0.0, cache_read=0.0875),
    "nova-micro": ModelPrice(
        0.035, 0.14, cache_write=0.0, cache_read=0.00875, max_output_tokens=5000
    ),
    "nova-lite": ModelPrice(0.06, 0.24, cache_write=0.0, cache_read=0.015, max_output_tokens=5000),
    "nova-pro": ModelPrice(0.80, 3.20, cache_write=0.0, cache_read=0.20, max_output_tokens=5000),
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
