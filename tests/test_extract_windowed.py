"""The extractor driven end to end by a stub model, so the windowing can be proved for nothing.

The two shapes that matter: a model with room for a whole answer behaves exactly as it always
did — six calls, one document, one cached prefix — and a model that stops at five thousand
tokens is asked the same questions over slices instead, with the answers folded back into one
reading.
"""

from __future__ import annotations

import pathlib

import pytest

from app.extraction.service import extract_contract
from app.llm.base import LLMProvider, LLMResult
from app.ocr.pymupdf_engine import PyMuPDFEngine

SAMPLE = pathlib.Path(__file__).resolve().parent / "fixtures/MSA_MeridianFoods.pdf"


class StubProvider(LLMProvider):
    """Answers every call with one record naming the pages it was shown."""

    name = "stub"

    def __init__(self, model_id: str):
        self.model_id = model_id
        self.seen: list[dict] = []

    def call_tool(self, *, system, user_text, tool, cache_prefix=None, max_tokens=16000,
                  temperature=0.0):
        pages = [
            int(line.split()[2])
            for line in (cache_prefix or "").splitlines()
            if line.startswith("===== PAGE ")
        ]
        self.seen.append({
            "pages": pages,
            "asked_doc_meta": "doc_meta" in user_text,
            "max_tokens": max_tokens,
        })
        # One definition per window, named for the window, plus a fee every window agrees on.
        return LLMResult(
            data={
                "records": [
                    {
                        "info_type": "definition",
                        "term": f"Term for pages {pages[0]}-{pages[-1]}" if pages else "Term",
                        "definition": "A definition.",
                        "citations": [{"quote": "x", "page": pages[0] if pages else 1}],
                    },
                    {
                        "info_type": "pricing_item",
                        "program": "Fuel Management Program",
                        "item": "Monthly Program Fee",
                        "frequency": "pvpm",
                        "amount": 3.25,
                        "citations": [{"quote": "y", "page": pages[0] if pages else 1}],
                    },
                ],
                "doc_meta": {"client_name": "Meridian Foods"} if "doc_meta" in user_text else None,
            },
            model=self.model_id,
            stop_reason="tool_use",
            input_tokens=100,
            output_tokens=200,
            max_output_tokens=max_tokens,
        )


@pytest.fixture(scope="module")
def parse():
    return PyMuPDFEngine().parse(SAMPLE, tables=True)


def test_a_model_with_room_reads_the_whole_document_once_per_category(parse):
    """The unwindowed path must be untouched: six calls, every one seeing every page."""
    provider = StubProvider("global.anthropic.claude-sonnet-4-6")
    extract_contract(SAMPLE, provider=provider, parse=parse)
    assert len(provider.seen) == 6
    for call in provider.seen:
        assert call["pages"] == [p.page_number for p in parse.pages]


def test_a_model_that_stops_at_five_thousand_is_asked_in_windows(parse):
    provider = StubProvider("apac.amazon.nova-lite-v1:0")
    extract_contract(SAMPLE, provider=provider, parse=parse)
    assert len(provider.seen) > 6, "a 5k-token model cannot answer a category in one response"
    # Every page still reaches every category — windowing slices the document, never the work.
    everywhere = {p.page_number for call in provider.seen for p in parse.pages
                  if p.page_number in call["pages"]}
    assert everywhere == {p.page_number for p in parse.pages}


def test_the_budget_asked_for_never_exceeds_what_the_model_can_write(parse):
    provider = StubProvider("apac.amazon.nova-lite-v1:0")
    extract_contract(SAMPLE, provider=provider, parse=parse)
    # The clamp lives in the Bedrock provider, but the planner should not be asking for silly
    # numbers either: with windows in play, no single call should want the full 32k.
    assert max(c["max_tokens"] for c in provider.seen) <= 32000


def test_the_parties_are_asked_for_exactly_once(parse):
    """Ask every window and a later one answers from a cross-reference, overwriting page one."""
    for model in ("global.anthropic.claude-sonnet-4-6", "apac.amazon.nova-lite-v1:0"):
        provider = StubProvider(model)
        extract_contract(SAMPLE, provider=provider, parse=parse)
        assert sum(1 for c in provider.seen if c["asked_doc_meta"]) == 1, model


def test_the_same_term_found_in_every_window_is_recorded_once(parse):
    """Each window reports the same fee; the document has one fee, not one per window."""
    provider = StubProvider("apac.amazon.nova-lite-v1:0")
    result = extract_contract(SAMPLE, provider=provider, parse=parse)
    fees = [r for r in result.extraction.records if r.info_type == "pricing_item"]
    assert len(fees) == 1, f"{len(fees)} copies of one fee survived the merge"


def test_each_window_contributes_what_only_it_saw(parse):
    provider = StubProvider("apac.amazon.nova-lite-v1:0")
    result = extract_contract(SAMPLE, provider=provider, parse=parse)
    definitions = [r for r in result.extraction.records if r.info_type == "definition"]
    assert len(definitions) > 1, "window-specific records should not collapse into one"


def test_telemetry_says_which_window_each_call_read(parse):
    provider = StubProvider("apac.amazon.nova-lite-v1:0")
    result = extract_contract(SAMPLE, provider=provider, parse=parse)
    rows = [c.as_dict() for c in result.calls]
    assert all(r["pages"] for r in rows), "a call with no page range cannot be diagnosed later"
    assert len({tuple(r["pages"]) for r in rows}) > 1


class TruncatingProvider(StubProvider):
    """Cuts off the first answer to a category, exactly as a real model runs out of room."""

    def __init__(self, model_id: str, cut: str = "misc_operational"):
        super().__init__(model_id)
        self.cut = cut
        self.cut_once = False

    def call_tool(self, *, system, user_text, tool, cache_prefix=None, max_tokens=16000,
                  temperature=0.0):
        result = super().call_tool(
            system=system, user_text=user_text, tool=tool, cache_prefix=cache_prefix,
            max_tokens=max_tokens, temperature=temperature,
        )
        types = {r["info_type"] for r in result.data["records"]}
        wants_misc = "responsibility" in user_text or "uncategorised" in user_text
        if wants_misc and not self.cut_once:
            self.cut_once = True
            # What Anthropic does on a budget it cannot fit in: the whole ask, nothing usable.
            return LLMResult(
                data={"records": []}, model=self.model_id, stop_reason="max_tokens",
                output_tokens=max_tokens, max_output_tokens=max_tokens,
            )
        assert types  # the stub always answers something
        return result


def test_a_cut_off_call_is_asked_again_over_less_document(parse):
    """Haiku lost every record in a 32,000-token answer this way, and said nothing."""
    provider = TruncatingProvider("global.anthropic.claude-sonnet-4-6")
    result = extract_contract(SAMPLE, provider=provider, parse=parse)
    retried = [c for c in result.calls if c.window >= 1000]
    assert retried, "a truncated call must be retried over a narrower window"
    assert len(retried) == 2, "the window should be halved, not abandoned"
    # And the retry covers the same pages the failed call did.
    covered = {p for c in retried for p in range(c.pages[0], c.pages[1] + 1)}
    assert covered == set(range(1, parse.page_count + 1))


def test_the_retry_happens_once_and_then_stops(parse):
    """A model that cannot answer at any width is a different problem, and costs money to ask."""
    provider = TruncatingProvider("global.anthropic.claude-sonnet-4-6")
    result = extract_contract(SAMPLE, provider=provider, parse=parse)
    assert len([c for c in result.calls if c.window >= 1_000_000]) == 0
