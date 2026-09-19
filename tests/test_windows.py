"""Cutting a contract into windows without cutting a term in half.

Every property here exists because breaking it loses money or loses a term. A page that appears
in no window is a page nobody read. A boundary through a fee table detaches a column header from
its amounts, and since a pricing term's identity includes its frequency, that does not merely
mislead — it mints a second term the deduplication cannot collapse.
"""

from __future__ import annotations

import pytest

from app.extraction.prompts import build_document_prefix
from app.extraction.windows import Window, page_tokens, page_windows, windows_for_output
from app.ocr.base import BBox, PageParse, TableParse


def _page(n: int, *, chars: int = 4000, table: tuple[float, float] | None = None) -> PageParse:
    """One page of `chars` characters, optionally carrying a table at (y0, y1)."""
    tables = []
    if table:
        y0, y1 = table
        tables = [TableParse(bbox=BBox(0.1, y0, 0.9, y1), rows=[["a", "b"], ["1", "2"]])]
    return PageParse(
        page_number=n, width=612, height=792, text="x" * chars, words=[], tables=tables
    )


def test_every_page_is_read_by_somebody():
    pages = [_page(n) for n in range(1, 21)]
    covered = {p.page_number for w in page_windows(pages) for p in w.pages}
    assert covered == {p.page_number for p in pages}


def test_windows_run_in_order_and_are_numbered():
    pages = [_page(n) for n in range(1, 21)]
    ws = page_windows(pages)
    assert [w.index for w in ws] == list(range(len(ws)))
    assert [w.first_page for w in ws] == sorted(w.first_page for w in ws)


def test_consecutive_windows_overlap_so_a_straddling_clause_is_whole_somewhere():
    pages = [_page(n) for n in range(1, 21)]
    ws = page_windows(pages, target_tokens=2000, overlap_pages=1)
    assert len(ws) > 2
    for earlier, later in zip(ws, ws[1:], strict=False):
        assert later.first_page <= earlier.last_page, "windows must overlap, not merely abut"


def test_overlap_can_be_turned_off():
    pages = [_page(n) for n in range(1, 21)]
    ws = page_windows(pages, target_tokens=2000, overlap_pages=0)
    for earlier, later in zip(ws, ws[1:], strict=False):
        assert later.first_page == earlier.last_page + 1


def test_a_table_running_across_a_page_break_holds_the_boundary_open():
    """The rule that protects the fee schedule, which is the one thing we may not get wrong."""
    pages = [
        _page(1, chars=4000),
        _page(2, chars=4000, table=(0.30, 0.95)),   # a table running to the foot of page 2
        _page(3, chars=4000, table=(0.05, 0.60)),   # ...and continuing at the head of page 3
        _page(4, chars=4000),
    ]
    # A target this small would ordinarily break after every page.
    ws = page_windows(pages, target_tokens=1000, max_tokens=9000, overlap_pages=0)
    holding = [w for w in ws if 2 in [p.page_number for p in w.pages]]
    assert len(holding) == 1
    assert 3 in [p.page_number for p in holding[0].pages], (
        "pages 2 and 3 share a table and must not be split"
    )


def test_a_table_that_merely_ends_near_the_foot_does_not_hold_the_boundary():
    pages = [
        _page(1, chars=4000),
        _page(2, chars=4000, table=(0.30, 0.95)),   # ends low...
        _page(3, chars=4000, table=(0.55, 0.80)),   # ...but the next starts mid-page
        _page(4, chars=4000),
    ]
    ws = page_windows(pages, target_tokens=1000, max_tokens=9000, overlap_pages=0)
    assert all(len(w.pages) == 1 for w in ws)


def test_holding_a_boundary_open_still_respects_the_hard_cap():
    """A table is worth a bigger window, not an unbounded one."""
    pages = [
        _page(1, chars=20000, table=(0.30, 0.99)),
        _page(2, chars=20000, table=(0.01, 0.60)),
    ]
    ws = page_windows(pages, target_tokens=2000, max_tokens=3000, overlap_pages=0)
    assert len(ws) == 2, "the cap wins when honouring the table would blow it"


def test_a_page_larger_than_the_cap_becomes_its_own_window():
    """Never split inside a page: the page marker is what a citation resolves against."""
    pages = [_page(1, chars=2000), _page(2, chars=80000), _page(3, chars=2000)]
    ws = page_windows(pages, target_tokens=1000, max_tokens=2000, overlap_pages=0)
    big = [w for w in ws if any(p.page_number == 2 for p in w.pages)]
    assert len(big) == 1 and len(big[0].pages) == 1


def test_a_trailing_stub_is_folded_back():
    """Greedy packing leaves the remainder alone at the end; an extra call for one thin page."""
    pages = [_page(n, chars=4000) for n in range(1, 10)] + [_page(10, chars=200)]
    ws = page_windows(pages, target_tokens=4000, max_tokens=12000, overlap_pages=0)
    assert ws[-1].tokens > 200, "the stub should have been folded into its neighbour"


def test_no_pages_means_no_windows():
    assert page_windows([]) == []


class TestSizingFromTheAnswer:
    """Windows are sized by how much the model will write, not by how much it will read."""

    def _pages(self, n=40, chars=4000):
        return [_page(i, chars=chars) for i in range(1, n + 1)]

    def test_an_answer_that_already_fits_is_asked_once(self):
        ws = windows_for_output(self._pages(), expected_output_tokens=2000, output_cap=5000)
        assert len(ws) == 1, "a category that fits in one response should not be split at all"

    def test_a_long_answer_is_split_enough_to_fit(self):
        ws = windows_for_output(self._pages(), expected_output_tokens=24000, output_cap=5000)
        assert len(ws) >= 24000 / (5000 * 0.6)
        # And each window should expect well under the cap, with room for a denser-than-average one.
        assert 24000 / len(ws) < 5000 * 0.75

    def test_a_generous_ceiling_needs_no_splitting(self):
        ws = windows_for_output(self._pages(), expected_output_tokens=24000, output_cap=64000)
        assert len(ws) == 1

    def test_windows_come_out_roughly_even(self):
        ws = windows_for_output(self._pages(), expected_output_tokens=24000, output_cap=5000)
        sizes = [w.tokens for w in ws]
        assert max(sizes) / min(sizes) < 3, f"lopsided windows: {sizes}"


def test_a_window_renders_with_the_documents_own_page_numbers():
    """A citation resolves by the number in its marker, so windows must never renumber."""
    pages = [_page(n) for n in range(1, 21)]
    ws = page_windows(pages, target_tokens=2000, overlap_pages=0)
    later = ws[2]
    text = build_document_prefix(later.pages, of_pages=len(pages))
    assert f"===== PAGE {later.first_page} =====" in text
    assert "===== PAGE 1 =====" not in text
    assert f"pages {later.first_page}-{later.last_page} of a 20-page" in text


def test_a_whole_document_prefix_says_nothing_about_windows():
    pages = [_page(n) for n in range(1, 5)]
    text = build_document_prefix(Window(0, pages).pages, of_pages=len(pages))
    assert "of a 4-page contract" not in text, (
        "a window covering everything is the whole document and should not claim to be a slice"
    )


@pytest.mark.parametrize("chars,expected", [(0, 0), (4000, 1000), (400, 100)])
def test_page_size_is_measured_in_tokens(chars, expected):
    assert page_tokens(_page(1, chars=chars)) == expected
