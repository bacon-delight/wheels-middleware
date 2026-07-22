"""Deterministic citation resolution against the real Schedule B anchors.

This is the split-screen highlight feature proven end-to-end with no LLM: given the
verbatim line-item text, we compute the exact normalized rectangle on the correct page.
"""

from __future__ import annotations

from app.ocr.citations import resolve, resolve_on_page

# The brief's demo anchors: Apex MLA Schedule B has $610.00, Meridian MLA has $465.00.
ANCHORS = {
    "MLA_ApexFieldServices.pdf": ("Delivery and freight: $610.00", "$610.00"),
    "MLA_MeridianFoods.pdf": ("Delivery and freight: $465.00", "$465.00"),
}


def test_full_row_quote_resolves_to_schedule_b_page(parsed_docs):
    """The distinctive full-row quote lands on page 11 (Schedule B), NOT the page-2 prose
    mention of 'delivery and freight charges'."""
    for filename, (row_quote, _amount) in ANCHORS.items():
        hit = resolve(parsed_docs[filename], row_quote)
        assert hit is not None, f"{filename}: full row quote not found"
        assert hit.page_number == 11, f"{filename}: expected page 11, got {hit.page_number}"
        # Label + amount on the same visual row => two rects grouped by line.
        assert len(hit.rects) >= 1
        for r in hit.rects:
            for v in (r.x0, r.y0, r.x1, r.y1):
                assert 0.0 <= v <= 1.0


def test_amount_alone_resolves_on_schedule_b(parsed_docs):
    for filename, (_row, amount) in ANCHORS.items():
        hit = resolve(parsed_docs[filename], amount, page_hint=11)
        assert hit is not None and hit.page_number == 11
        assert amount.replace("$", "") in hit.matched_text or "$" in hit.matched_text


def test_page_hint_is_respected_and_falls_through_when_wrong(parsed_docs):
    doc = parsed_docs["MLA_ApexFieldServices.pdf"]
    # A wrong hint should still find the real location by falling through to a full scan.
    hit = resolve(doc, "Delivery and freight: $610.00", page_hint=3)
    assert hit is not None and hit.page_number == 11


def test_unfindable_quote_returns_none(parsed_docs):
    doc = parsed_docs["MLA_ApexFieldServices.pdf"]
    assert resolve(doc, "this exact phrase does not appear anywhere xyzzy") is None


def test_resolution_is_whitespace_and_punctuation_insensitive(parsed_docs):
    """Dot-leaders / spacing / colons must not defeat matching."""
    doc = parsed_docs["MLA_ApexFieldServices.pdf"]
    page11 = next(p for p in doc.pages if p.page_number == 11)
    for variant in ("Delivery and freight $610.00", "delivery  and   freight:$610.00"):
        assert resolve_on_page(page11, variant) is not None, variant
