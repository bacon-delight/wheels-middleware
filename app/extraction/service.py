"""Extraction orchestration: parse -> LLM tool-use -> deterministic citation resolution.

One tool-use call extracts the whole document (all 13 service lines + lease terms). The
model's reported coordinates are ignored; we locate each verbatim quote in the parsed word
geometry and attach the true normalized bbox, then flag low-confidence / unlocatable fields
for analyst review.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings, get_settings
from ..llm.base import LLMProvider
from ..llm.tools import CONTRACT_TOOL
from ..ocr.base import DocumentParse, OCREngine, Source
from ..ocr.citations import resolve as resolve_citation
from ..ocr.pymupdf_engine import PyMuPDFEngine
from .prompts import SYSTEM_PROMPT, build_user_content
from .schema import BBoxModel, Citation, ContractExtraction


@dataclass
class FieldFlag:
    path: str  # human-readable location, e.g. "Collision / fee_items[0]"
    reason: str


@dataclass
class ExtractionResult:
    extraction: ContractExtraction
    parse: DocumentParse
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    needs_review: list[FieldFlag] = field(default_factory=list)
    unresolved_citations: int = 0


def _resolve_citations(
    citations: list[Citation], parse: DocumentParse
) -> tuple[int, int]:
    """Attach normalized bbox to each citation in place. Returns (resolved, total)."""
    resolved = 0
    for c in citations:
        hit = resolve_citation(parse, c.quote, page_hint=c.page)
        if hit is not None:
            c.page = hit.page_number
            c.bbox = BBoxModel(**hit.bbox.as_dict())
            c.engine = parse.engine
            resolved += 1
    return resolved, len(citations)


def resolve_all_citations(
    extraction: ContractExtraction, parse: DocumentParse
) -> tuple[int, int]:
    resolved = total = 0
    for sl in extraction.service_lines:
        r, t = _resolve_citations(sl.citations, parse)
        resolved += r
        total += t
    for bf in extraction.bundled_fees:
        r, t = _resolve_citations(bf.citations, parse)
        resolved += r
        total += t
    if extraction.lease_terms:
        r, t = _resolve_citations(extraction.lease_terms.citations, parse)
        resolved += r
        total += t
    return resolved, total


def _collect_flags(
    extraction: ContractExtraction, threshold: float
) -> list[FieldFlag]:
    flags: list[FieldFlag] = []
    for sl in extraction.service_lines:
        if not sl.elected:
            continue
        if sl.confidence <= threshold:
            flags.append(FieldFlag(path=str(sl.service), reason=f"confidence {sl.confidence:.2f}"))
        if sl.fee_items and not sl.citations:
            flags.append(FieldFlag(path=str(sl.service), reason="fee items without a citation"))
    return flags


def extract_contract(
    source: Source,
    *,
    doc_type_hint: str | None = None,
    provider: LLMProvider | None = None,
    engine: OCREngine | None = None,
    settings: Settings | None = None,
) -> ExtractionResult:
    settings = settings or get_settings()
    engine = engine or PyMuPDFEngine()
    parse = engine.parse(source)

    if provider is None:
        from ..llm.factory import get_provider

        provider = get_provider(settings)

    user_text = build_user_content(parse, doc_type_hint=doc_type_hint)
    llm_result = provider.call_tool(system=SYSTEM_PROMPT, user_text=user_text, tool=CONTRACT_TOOL)
    extraction = ContractExtraction.model_validate(llm_result.data)

    resolved, total = resolve_all_citations(extraction, parse)
    flags = _collect_flags(extraction, settings.review_confidence_threshold)

    return ExtractionResult(
        extraction=extraction,
        parse=parse,
        input_tokens=llm_result.input_tokens,
        output_tokens=llm_result.output_tokens,
        model=llm_result.model,
        needs_review=flags,
        unresolved_citations=total - resolved,
    )
