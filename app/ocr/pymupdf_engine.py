"""PyMuPDF (fitz) engine — native-text extraction with word-level geometry.

This is the $0, no-GPU launch path. It gives us both the formatted page render
(PNG) for the left panel and the exact word bounding boxes the citation resolver
needs for the split-screen highlight — all in-Lambda.
"""

from __future__ import annotations

try:  # PyMuPDF >= 1.24 exposes `pymupdf`; older wheels only expose `fitz`.
    import pymupdf
except ImportError:  # pragma: no cover
    import fitz as pymupdf

from .base import BBox, DocumentParse, OCREngine, PageParse, Source, Word


def _open(source: Source):
    if isinstance(source, bytes):
        return pymupdf.open(stream=source, filetype="pdf")
    return pymupdf.open(str(source))


class PyMuPDFEngine(OCREngine):
    name = "pymupdf"

    def parse(self, source: Source) -> DocumentParse:
        doc = _open(source)
        try:
            pages: list[PageParse] = []
            for i in range(doc.page_count):
                page = doc[i]
                rect = page.rect
                w, h = rect.width, rect.height
                words: list[Word] = []
                # get_text("words") -> (x0, y0, x1, y1, "word", block_no, line_no, word_no)
                for x0, y0, x1, y1, text, block_no, line_no, _word_no in page.get_text("words"):
                    if not text.strip():
                        continue
                    words.append(
                        Word(
                            text=text,
                            bbox=BBox(x0 / w, y0 / h, x1 / w, y1 / h),
                            line=block_no * 10_000 + line_no,
                        )
                    )
                pages.append(
                    PageParse(
                        page_number=i + 1,
                        width=w,
                        height=h,
                        text=page.get_text("text"),
                        words=words,
                    )
                )
            return DocumentParse(engine=self.name, pages=pages)
        finally:
            doc.close()

    def has_text_layer(self, source: Source) -> bool:
        doc = _open(source)
        try:
            # A native contract has selectable text on essentially every page; a scanned
            # PDF yields little/none. Threshold keeps a stray text-watermark from fooling us.
            chars = sum(len(doc[i].get_text("text").strip()) for i in range(doc.page_count))
            return chars > 50 * max(doc.page_count, 1)
        finally:
            doc.close()

    def render_page_png(self, source: Source, page_number: int, dpi: int = 150) -> bytes:
        doc = _open(source)
        try:
            page = doc[page_number - 1]
            pix = page.get_pixmap(dpi=dpi)
            return pix.tobytes("png")
        finally:
            doc.close()
