"""The PyMuPDF engine parses the real sample PDFs into normalized word geometry."""

from __future__ import annotations

import pytest

PAGE_COUNTS = {
    "MSA_MeridianFoods.pdf": 16,
    "MLA_MeridianFoods.pdf": 14,
    "MSA_ApexFieldServices.pdf": 16,
    "MLA_ApexFieldServices.pdf": 14,
}


@pytest.mark.parametrize("filename,pages", PAGE_COUNTS.items())
def test_page_counts(parsed_docs, filename, pages):
    assert parsed_docs[filename].page_count == pages


@pytest.mark.parametrize("filename", PAGE_COUNTS)
def test_all_docs_are_native_text(engine, docs_dir, filename):
    assert engine.has_text_layer(docs_dir / filename) is True


@pytest.mark.parametrize("filename", PAGE_COUNTS)
def test_pages_have_words_with_normalized_bboxes(parsed_docs, filename):
    doc = parsed_docs[filename]
    total_words = 0
    for page in doc.pages:
        assert page.text.strip(), f"empty page {page.page_number} in {filename}"
        for w in page.words:
            total_words += 1
            b = w.bbox
            for v in (b.x0, b.y0, b.x1, b.y1):
                assert 0.0 <= v <= 1.0, f"bbox out of [0,1] in {filename}: {b}"
            assert b.x1 >= b.x0 and b.y1 >= b.y0
    assert total_words > 1000, f"suspiciously few words in {filename}: {total_words}"


def test_render_page_png(engine, docs_dir):
    png = engine.render_page_png(docs_dir / "MLA_ApexFieldServices.pdf", 11, dpi=100)
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    assert len(png) > 5000
