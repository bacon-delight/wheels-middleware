"""Cut a contract into page windows a small model can answer about.

The extractor asks one question per category and expects the whole answer at once. On a 41-page
contract the responsibilities alone come back as 24,000 tokens, and the cheap models on Bedrock
stop at 5,000 — which is the only thing standing between this system and reading a contract for
three cents instead of a dollar.

A window is a run of consecutive pages. Ask the same question of six windows and you get six
small answers rather than one large one. Nothing is routed: every category sees every page, so
no decision about which pages matter can quietly lose a term.

Two rules do the real work.

**Size by tokens, not by page count.** A page of dense fee tables holds ten times the text of a
signature page, so a fixed "five pages per window" produces windows that vary by an order of
magnitude and either waste the budget or overflow it.

**Never break a table across a boundary.** In a pricing schedule the column an amount sits under
is its frequency. Split the header away from its rows and the model reads $15.00 with no idea
whether it is monthly or one-off — and because a pricing term's identity includes its frequency,
the wrong answer does not merely mislead, it mints a second term that no deduplication collapses.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..ocr.base import PageParse

# A table whose box runs to the bottom of one page, continuing into one that starts at the top of
# the next, is one table. Generous margins: a false positive costs a slightly larger window, a
# false negative costs a detached column header.
_BOTTOM = 0.90
_TOP = 0.14


@dataclass(frozen=True)
class Window:
    """One slice of a contract, and where it sits in the whole."""

    index: int
    pages: list[PageParse]

    @property
    def first_page(self) -> int:
        return self.pages[0].page_number

    @property
    def last_page(self) -> int:
        return self.pages[-1].page_number

    @property
    def tokens(self) -> int:
        return sum(page_tokens(p) for p in self.pages)

    def __repr__(self) -> str:  # a window is read in logs far more often than in a debugger
        return f"<Window {self.index}: pages {self.first_page}-{self.last_page}, {self.tokens:,}t>"


def page_tokens(page: PageParse) -> int:
    """Roughly what this page costs to send, text and rendered tables together.

    Four characters to the token is crude, but it is the same crudeness everywhere and the
    windowing only needs pages to be comparable with one another.
    """
    chars = len(page.text or "")
    for table in page.tables or []:
        chars += sum(len(cell or "") + 3 for row in table.rows for cell in row)
    return chars // 4


def _table_continues(page: PageParse, nxt: PageParse) -> bool:
    """Does a table on `page` run on into `nxt`?"""
    ends_low = any(t.bbox.y1 >= _BOTTOM for t in page.tables or [])
    starts_high = any(t.bbox.y0 <= _TOP for t in nxt.tables or [])
    return ends_low and starts_high


def page_windows(
    pages: Sequence[PageParse],
    *,
    target_tokens: int = 6000,
    max_tokens: int = 9000,
    overlap_pages: int = 1,
) -> list[Window]:
    """Split pages into overlapping windows of roughly `target_tokens` each.

    `overlap_pages` repeats the tail of one window at the head of the next, so a clause that
    straddles a page break is whole in at least one of them. The duplicate records that produces
    are collapsed downstream by the term id, which is a hash of what the term says.

    A single page larger than `max_tokens` becomes its own window rather than being split: page
    boundaries are the only place a citation's page marker stays attached to its text.
    """
    pages = list(pages)
    if not pages:
        return []

    windows: list[list[PageParse]] = []
    current: list[PageParse] = []
    running = 0

    for i, page in enumerate(pages):
        size = page_tokens(page)
        would_overflow = current and running + size > target_tokens
        # Hold the boundary open when a table spans it, unless doing so would blow the hard cap.
        if would_overflow and i > 0 and _table_continues(pages[i - 1], page):
            would_overflow = running + size > max_tokens
        if would_overflow:
            windows.append(current)
            current, running = [], 0
        current.append(page)
        running += size

    if current:
        windows.append(current)

    # Greedy packing leaves whatever is left over as the final window, which on a document that
    # divides badly is a stub of one thin page — an extra call, and an answer with almost no
    # context around it. Fold it back if the result still fits.
    if len(windows) > 1:
        tail = sum(page_tokens(p) for p in windows[-1])
        prev = sum(page_tokens(p) for p in windows[-2])
        if tail < target_tokens * 0.5 and tail + prev <= max_tokens:
            windows[-2].extend(windows.pop())

    # Overlap is applied afterwards so it can never change where a boundary was chosen.
    out: list[Window] = []
    for n, block in enumerate(windows):
        if n > 0 and overlap_pages:
            tail = windows[n - 1][-overlap_pages:]
            block = [p for p in tail if p is not block[0]] + block
        out.append(Window(index=n, pages=block))
    return out


def windows_for_output(
    pages: Sequence[PageParse],
    expected_output_tokens: int,
    output_cap: int,
    *,
    fill: float = 0.6,
    overlap_pages: int = 1,
) -> list[Window]:
    """Enough windows that each answer fits inside `output_cap`.

    Sizing runs backwards from the answer rather than the question: what matters is not how much
    document a window holds but how much the model will write about it. `fill` leaves room for a
    window that is denser than the document's average — aiming at the cap exactly would make
    every unusually rich window an overflow.
    """
    if expected_output_tokens <= 0 or output_cap <= 0:
        return [Window(index=0, pages=list(pages))]
    need = max(1, -(-expected_output_tokens // int(output_cap * fill)))  # ceil
    if need == 1:
        return [Window(index=0, pages=list(pages))]
    total = sum(page_tokens(p) for p in pages)
    target = max(1, total // need)
    return page_windows(
        pages,
        target_tokens=target,
        max_tokens=int(target * 1.5),
        overlap_pages=overlap_pages,
    )
