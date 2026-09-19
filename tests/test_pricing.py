"""What a run costs, and how we know a response was cut off.

Three of the six rates in this table were wrong when it was audited — Opus by 3x and Sonnet 5 by
50% — under a comment claiming they had been checked that morning. The table is the only thing
standing between a person and a confidently wrong number, so every rate is pinned here and
the measured Sonnet run is reproduced to the cent.
"""

from __future__ import annotations

import pytest

from app.llm.base import LLMResult
from app.llm.pricing import cost_usd, price_for, uncached_cost_usd

# input, output, cache write, cache read — USD per million tokens, AWS Price List, 2026-09-19.
RATES = [
    ("global.anthropic.claude-sonnet-4-6", 3.00, 15.00, 3.75, 0.30),
    ("global.anthropic.claude-sonnet-5", 2.00, 10.00, 2.50, 0.20),
    ("global.anthropic.claude-opus-5", 5.00, 25.00, 6.25, 0.50),
    ("global.anthropic.claude-haiku-4-5-20251001-v1:0", 1.00, 5.00, 1.25, 0.10),
    # Nova pays nothing to write a cache and a quarter of input to read one — not the Anthropic
    # ratios, which is exactly the mistake the old derived multipliers would have made.
    ("global.amazon.nova-2-lite-v1:0", 0.35, 2.95, 0.00, 0.0875),
    ("apac.amazon.nova-lite-v1:0", 0.06, 0.24, 0.00, 0.015),
    ("apac.amazon.nova-micro-v1:0", 0.035, 0.14, 0.00, 0.00875),
]


@pytest.mark.parametrize("model,inp,out,cw,cr", RATES)
def test_every_rate_is_pinned(model, inp, out, cw, cr):
    p = price_for(model)
    assert p is not None, f"{model} has no price, so its cost would report as unknown"
    assert p.input_per_mtok == inp
    assert p.output_per_mtok == out
    assert p.cache_write_per_mtok == pytest.approx(cw)
    assert p.cache_read_per_mtok == pytest.approx(cr)


def test_a_family_name_is_not_matched_by_a_shorter_one():
    """`nova-2-lite` must not resolve to `nova-lite`, which is 12x cheaper on output."""
    assert price_for("global.amazon.nova-2-lite-v1:0").output_per_mtok == 2.95
    assert price_for("apac.amazon.nova-lite-v1:0").output_per_mtok == 0.24


@pytest.mark.parametrize(
    "model,cap",
    [
        ("apac.amazon.nova-lite-v1:0", 5000),
        ("apac.amazon.nova-micro-v1:0", 5000),
        ("global.amazon.nova-2-lite-v1:0", 64000),
        ("global.anthropic.claude-sonnet-4-6", 64000),
    ],
)
def test_the_output_ceiling_is_recorded(model, cap):
    """The extractor asks for 32,000 tokens a call; a 5,000-token model must say so."""
    assert price_for(model).max_output_tokens == cap


def test_an_unknown_model_costs_nothing_rather_than_something_wrong():
    assert price_for("meta.llama-99") is None
    assert cost_usd("meta.llama-99", input_tokens=1000, output_tokens=1000) is None


# The mean of the four real contract runs in dev — 30 to 41 pages, ~350 terms each.
MEASURED = dict(
    input_tokens=3416, output_tokens=58910,
    cache_read_tokens=237161, cache_write_tokens=47432,
)


def test_the_measured_sonnet_run_reconciles():
    """What those four runs actually cost, to the cent: $1.14."""
    assert cost_usd("global.anthropic.claude-sonnet-4-6", **MEASURED) == pytest.approx(
        1.143, abs=0.002
    )


def test_caching_is_what_makes_the_six_call_design_affordable():
    cached = cost_usd("global.anthropic.claude-sonnet-4-6", **MEASURED)
    uncached = uncached_cost_usd("global.anthropic.claude-sonnet-4-6", **MEASURED)
    assert uncached == pytest.approx(1.748, abs=0.002)
    assert (1 - cached / uncached) == pytest.approx(0.35, abs=0.02)


def test_the_same_workload_on_nova_lite_clears_the_target():
    """The whole point of the exercise: a tenth of the Sonnet bill, or better."""
    # Priced without any caching at all, which is the conservative case for Nova: every window
    # sends its own pages, nothing is reused.
    on_nova = cost_usd(
        "apac.amazon.nova-lite-v1:0",
        input_tokens=290000, output_tokens=58910, cache_read_tokens=0, cache_write_tokens=0,
    )
    assert on_nova < 0.10, f"${on_nova:.3f} is over the target that justifies this work"


class TestTruncationIsDetectedWhateverTheProviderCallsIt:
    """A cut-off answer looks exactly like a contract with fewer terms. Telling them apart is
    the single most important signal in the pipeline, and it used to be one string literal."""

    def _r(self, **kw):
        return LLMResult({}, max_output_tokens=4000, **kw)

    def test_a_clean_finish_is_not_truncated(self):
        assert not self._r(stop_reason="tool_use", output_tokens=500).truncated
        assert not self._r(stop_reason="end_turn", output_tokens=500).truncated

    @pytest.mark.parametrize("reason", ["max_tokens", "MAX_TOKENS", "length", "model_length"])
    def test_every_spelling_of_running_out(self, reason):
        assert self._r(stop_reason=reason, output_tokens=900).truncated

    def test_a_response_that_used_its_whole_budget_is_cut_off_whatever_it_says(self):
        assert self._r(stop_reason="tool_use", output_tokens=3990).truncated

    def test_a_salvaged_tail_marks_the_call(self):
        assert self._r(stop_reason="tool_use", output_tokens=100, salvaged=True).truncated

    def test_a_provider_that_reports_nothing_is_judged_on_its_budget(self):
        # Flagging every such call would make the signal meaningless; the budget test still works.
        assert not self._r(stop_reason=None, output_tokens=100).truncated
        assert self._r(stop_reason=None, output_tokens=3999).truncated
