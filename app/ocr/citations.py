"""Deterministic citation resolution.

The LLM returns a verbatim `quote` (and optionally a page hint). We do NOT trust
model-reported coordinates; instead we locate the quote inside the engine's word
geometry and compute the highlight rectangle(s) ourselves. This is exact for
native text and works identically on Textract word output.

Matching is whitespace/punctuation-insensitive: we normalize both the page and the
quote to a compact character stream (letters, digits, and money/rate glyphs only)
and substring-match, mapping matched characters back to the words that produced
them. Matched words are grouped by visual line so a phrase spanning two lines
yields one rectangle per line rather than one giant box.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import BBox, DocumentParse, PageParse, Word

# Characters kept during normalization. We keep letters, digits, and money/rate glyphs but
# deliberately DROP '.' and ',' so that table dot-leaders ("......") and thousands separators
# ("$1,250") don't break a full-row match; "$610.00" and "$610" still normalize distinctly
# ("$61000" vs "$610"). Everything else (spaces, colons, parens) is dropped too.
_KEEP = set("abcdefghijklmnopqrstuvwxyz0123456789$%-/")


def _norm(s: str) -> str:
    return "".join(c for c in s.lower() if c in _KEEP)


@dataclass
class ResolvedCitation:
    page_number: int
    rects: list[BBox]  # one per visual line; normalized [0,1]
    matched_text: str
    score: float  # 1.0 exact normalized match; <1.0 best-effort

    @property
    def bbox(self) -> BBox:
        r = self.rects[0]
        for other in self.rects[1:]:
            r = r.union(other)
        return r


def _build_page_index(page: PageParse) -> tuple[str, list[int]]:
    """Return (normalized page string, char->word-index map)."""
    buf: list[str] = []
    char_to_word: list[int] = []
    for wi, word in enumerate(page.words):
        for c in _norm(word.text):
            buf.append(c)
            char_to_word.append(wi)
    return "".join(buf), char_to_word


def _rects_for_words(words: list[Word], indices: set[int]) -> list[BBox]:
    """Union the bboxes of the selected words, grouped by visual line."""
    by_line: dict[int, BBox] = {}
    for wi in sorted(indices):
        w = words[wi]
        if w.line in by_line:
            by_line[w.line] = by_line[w.line].union(w.bbox)
        else:
            by_line[w.line] = w.bbox
    # Order lines top-to-bottom for a natural highlight sequence.
    return [by_line[k] for k in sorted(by_line, key=lambda k: by_line[k].y0)]


def resolve_on_page(page: PageParse, quote: str) -> ResolvedCitation | None:
    q = _norm(quote)
    if not q:
        return None
    page_norm, char_to_word = _build_page_index(page)
    pos = page_norm.find(q)
    if pos == -1:
        return None
    word_indices = set(char_to_word[pos : pos + len(q)])
    rects = _rects_for_words(page.words, word_indices)
    matched = " ".join(page.words[i].text for i in sorted(word_indices))
    return ResolvedCitation(
        page_number=page.page_number, rects=rects, matched_text=matched, score=1.0
    )


def resolve(
    doc: DocumentParse, quote: str, page_hint: int | None = None
) -> ResolvedCitation | None:
    """Locate `quote` in the document, preferring the hinted page if given."""
    if page_hint is not None:
        for page in doc.pages:
            if page.page_number == page_hint:
                hit = resolve_on_page(page, quote)
                if hit:
                    return hit
                break  # hint was wrong; fall through to a full scan
    for page in doc.pages:
        if page.page_number == page_hint:
            continue
        hit = resolve_on_page(page, quote)
        if hit:
            return hit
    return None
