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

from .base import BBox, DocumentParse, OCREngine, PageParse, Source, TableParse, Word


def _open(source: Source):
    if isinstance(source, bytes):
        return pymupdf.open(stream=source, filetype="pdf")
    return pymupdf.open(str(source))


def _tables(page, w: float, h: float) -> list[TableParse]:
    """Detected tables on one page, with cells kept exactly as found.

    Detection is best-effort by nature: a contract that lays its pricing out in prose yields
    nothing here, and that is fine — tables are an extra signal on top of the page text, never
    a replacement for it. A failure to detect must never fail the parse.
    """
    try:
        found = page.find_tables()
    except Exception:  # noqa: BLE001 - a page we cannot analyse still has usable text
        return []
    out: list[TableParse] = []
    for table in getattr(found, "tables", []) or []:
        try:
            rows = [[(c or "") for c in row] for row in table.extract()]
        except Exception:  # noqa: BLE001
            continue
        if not rows:
            continue
        x0, y0, x1, y1 = table.bbox
        out.append(
            TableParse(
                bbox=BBox(x0 / w, y0 / h, x1 / w, y1 / h),
                rows=rows,
                header=[(c or "") for c in (table.header.names if table.header else [])],
            )
        )
    return out


class PyMuPDFEngine(OCREngine):
    name = "pymupdf"

    def parse(self, source: Source, *, tables: bool = False) -> DocumentParse:
        """Parse the document. `tables` is opt-in because table detection is the expensive part.

        The parse worker only needs text (to classify) and word geometry (to render), so it
        leaves tables off; the extract worker turns them on, and the detection runs once per
        document rather than twice.
        """
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
                        tables=_tables(page, w, h) if tables else [],
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
