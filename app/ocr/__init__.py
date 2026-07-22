"""Pluggable OCR / document-parsing layer.

The UI highlight contract is engine-agnostic: every engine produces normalized
word geometry (BBox in [0,1] page fractions), so the citation resolver and the
front-end overlay work identically whether coordinates came from PyMuPDF (native
text) or Textract (scanned).
"""

from .base import BBox, DocumentParse, OCREngine, PageParse, Word
from .pymupdf_engine import PyMuPDFEngine

__all__ = [
    "BBox",
    "DocumentParse",
    "OCREngine",
    "PageParse",
    "Word",
    "PyMuPDFEngine",
]
