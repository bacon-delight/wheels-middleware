"""Folding many windows' answers into one reading, without losing or inventing a term.

Each case here is a way the document can be read twice. The expensive mistakes are silent ones:
half a price schedule deleted because two readings hashed alike, or a fee that exists only
because one window saw the sentence and not the table.
"""

from __future__ import annotations

from app.extraction.merge import merge_records


def _pricing(**kw):
    base = {
        "info_type": "pricing_item",
        "program": "Fuel Management Program",
        "item": "Monthly Program Fee",
        "frequency": "pvpm",
        "citations": [],
    }
    return {**base, **kw}


def _definition(term="Driver", text="A person who drives.", quote="q"):
    return {
        "info_type": "definition", "term": term, "definition": text,
        "citations": [{"quote": quote, "page": 1}],
    }


class TestWhatCollapses:
    def test_one_definition_read_by_two_windows_is_one_row(self):
        out, report = merge_records([_definition(quote="a"), _definition(quote="b")])
        assert len(out) == 1
        assert report.collapsed == 1

    def test_punctuation_and_case_do_not_mint_a_second_row(self):
        out, _ = merge_records([_definition(term="Driver"), _definition(term="driver.")])
        assert len(out) == 1

    def test_both_windows_citations_survive_the_collapse(self):
        """Evidence is the product. Keeping one window's citation would throw away a page."""
        out, _ = merge_records([_definition(quote="a"), _definition(quote="b")])
        assert {c["quote"] for c in out[0]["citations"]} == {"a", "b"}

    def test_the_fuller_reading_wins_and_takes_what_the_thinner_one_knew(self):
        thin = _pricing(amount=15.0)
        full = _pricing(amount=15.0, calculation="15.00 per vehicle per month", notes=["x"])
        out, _ = merge_records([thin, full])
        assert len(out) == 1
        assert out[0]["calculation"] == "15.00 per vehicle per month"

    def test_a_signature_stamped_on_every_page_is_recorded_once(self):
        stamp = {"info_type": "signature", "company": "Wheels, LLC", "name": "A Signer",
                 "citations": [{"quote": "DocuSign Envelope", "page": 1}]}
        out, report = merge_records([dict(stamp) for _ in range(12)])
        assert len(out) == 1 and report.collapsed == 11


class TestWhatMustNotCollapse:
    def test_a_tiered_fee_split_across_a_boundary_keeps_every_band(self):
        """The expensive one: identical ids, and a naive merge deletes half the schedule."""
        low = _pricing(amount=15.0, tier_bands=[{"min_units": 1, "max_units": 250, "amount": 15.0}])
        high = _pricing(amount=15.0, tier_bands=[{"min_units": 251, "max_units": None, "amount": 12.0}])
        out, report = merge_records([low, high])
        assert len(out) == 1
        assert len(out[0]["tier_bands"]) == 2, "a band was dropped; that is money lost"
        assert report.unioned == 1

    def test_two_different_programs_stay_two_records(self):
        out, _ = merge_records(
            [_pricing(amount=3.25), _pricing(program="Toll Management Program", amount=1.75)]
        )
        assert len(out) == 2

    def test_the_same_fee_at_two_amounts_is_reported_rather_than_guessed(self):
        out, report = merge_records([_pricing(amount=15.0), _pricing(amount=12.0)])
        assert len(out) == 2, "neither reading may be silently preferred"
        assert report.amount_conflicts, "a disagreement about money must be named"

    def test_a_line_read_once_with_its_price_and_once_without_is_flagged(self):
        out, report = merge_records([_pricing(amount=15.0), _pricing(amount=None)])
        assert report.amount_conflicts
        assert any("15.0" in c for c in report.amount_conflicts)


class TestTheBareProgramRule:
    def test_a_program_priced_elsewhere_is_not_also_an_unpriced_enrolment(self):
        named_only = {"info_type": "pricing_item", "program": "Fuel Management Program",
                      "item": None, "citations": []}
        out, report = merge_records([_pricing(amount=3.25), named_only])
        assert len(out) == 1 and report.bare_dropped == 1

    def test_a_program_that_really_is_priced_nowhere_keeps_its_record(self):
        """This is how we know a customer is enrolled in something charged at no cost."""
        named_only = {"info_type": "pricing_item", "program": "Roadside Assistance",
                      "item": None, "citations": []}
        out, report = merge_records([_pricing(amount=3.25), named_only])
        assert len(out) == 2 and report.bare_dropped == 0


def test_an_empty_run_merges_to_nothing():
    out, report = merge_records([])
    assert out == [] and report.kept == 0


def test_the_report_counts_what_survived():
    out, report = merge_records([_definition(), _definition(), _pricing(amount=1.0)])
    assert report.kept == len(out) == 2
