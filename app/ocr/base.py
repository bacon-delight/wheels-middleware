"""Engine-agnostic document-parse data model.

Coordinates are ALWAYS normalized to [0,1] as fractions of page width/height with
origin at the top-left, so the UI overlay scales to any render zoom and never
depends on which engine produced them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class BBox:
    x0: float
    y0: float
    x1: float
    y1: float

    def union(self, other: BBox) -> BBox:
        return BBox(
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )

    def as_dict(self) -> dict[str, float]:
        return {"x0": self.x0, "y0": self.y0, "x1": self.x1, "y1": self.y1}


@dataclass
class Word:
    text: str
    bbox: BBox
    line: int  # stable per-visual-line id, used to group multi-line highlights


@dataclass
class PageParse:
    page_number: int  # 1-based
    width: float  # points, native page size
    height: float
    text: str  # full page text in reading order
    words: list[Word] = field(default_factory=list)
    render_key: str | None = None  # S3 key / local path of the page PNG, when rendered


@dataclass
class DocumentParse:
    engine: str
    pages: list[PageParse] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)

    @property
    def page_count(self) -> int:
        return len(self.pages)


Source = str | Path | bytes


class OCREngine(ABC):
    """A document-parsing engine. Implementations must populate normalized geometry."""

    name: str

    @abstractmethod
    def parse(self, source: Source) -> DocumentParse:
        """Parse a PDF into pages with normalized word geometry."""

    @abstractmethod
    def has_text_layer(self, source: Source) -> bool:
        """True if the PDF carries an extractable text layer (native), else it is scanned."""

    def render_page_png(self, source: Source, page_number: int, dpi: int = 150) -> bytes:
        """Optional: render a page to PNG bytes for the split-screen left panel."""
        raise NotImplementedError
