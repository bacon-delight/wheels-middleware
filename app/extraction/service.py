"""Extraction orchestration: parse -> five cached tool-use calls -> citation resolution.

Five calls, one per category, over a single cached copy of the document. A contract holds far
more than one response can carry, so the work is split by category; the document is sent once at
full price and read four more times from cache.

The model's reported coordinates are never trusted. Each verbatim quote is located in the parsed
word geometry and the true normalised box attached, which is also why a paraphrased quote is
worse than no quote.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from ..llm.base import CLEAN_STOP_REASONS, LLMProvider, LLMResult, OutputOverflow
from ..llm.pricing import cost_usd, price_for, uncached_cost_usd
from ..llm.tools import CONTRACT_TOOL
from ..ocr.base import DocumentParse, OCREngine, Source
from ..ocr.citations import resolve as resolve_citation
from ..ocr.pymupdf_engine import PyMuPDFEngine
from .merge import merge_records
from .prompts import SYSTEM_PROMPT, build_call_instruction, build_document_prefix
from .schema import (
    SCHEMA_VERSION,
    BBoxModel,
    Citation,
    ContractExtraction,
    DocumentMeta,
    parse_records,
)
from .windows import Window, windows_for_output

log = logging.getLogger(__name__)

# How many output tokens each call may produce. Pricing and the reference half of misc are the
# large ones: a contract can carry forty definitions, each quoting itself verbatim.
# Measured against the Walmart statement of work, where the first run cut three of five calls
# off at their limit and lost them. Sonnet allows far more than this; the cost of a generous
# budget is nothing unless it is used, whereas the cost of a tight one is a category of the
# contract silently missing.
CALL_BUDGET: dict[str, int] = {
    "pricing": 32000,
    "sla": 24000,
    "reporting": 24000,
    "definitions": 32000,
    "misc_reference": 24000,
    "misc_operational": 32000,
}

# The order matters. The first call writes the cache; the rest read it. Firing all five at once
# would have them all miss, because an entry only becomes readable once the first response
# begins.
CACHE_PRIMING_CALL = "pricing"


@dataclass
class FieldFlag:
    path: str
    reason: str


@dataclass
class CallTelemetry:
    """What one call to the model cost and whether it came back whole."""

    call: str
    records: int = 0
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    stop_reason: str | None = None
    latency_ms: int | None = None
    cost_usd: float | None = None
    error: str | None = None
    # Which slice of the contract this call read. Window 0 covering the whole document is the
    # unwindowed case, which is what a model with room for the whole answer still does.
    window: int = 0
    pages: tuple[int, int] | None = None
    max_output_tokens: int | None = None
    salvaged: bool = False
    # The model ran out of room mid-answer and returned nothing. Distinct from a failure,
    # because the remedy is a smaller window rather than a different model.
    overflowed: bool = False

    @property
    def truncated(self) -> bool:
        """Did this call come back cut off?

        Mirrors LLMResult.truncated rather than matching one provider's word for it: an
        unrecognised stop reason, an answer that used its whole budget, a tail the parser had to
        rescue, or an overflow that returned nothing at all.
        """
        if self.overflowed or self.salvaged:
            return True
        if self.stop_reason and self.stop_reason.lower() not in CLEAN_STOP_REASONS:
            return True
        if self.max_output_tokens and (self.output_tokens or 0) >= self.max_output_tokens - 32:
            return True
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "call": self.call,
            "records": self.records,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "stop_reason": self.stop_reason,
            "truncated": self.truncated,
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
            "error": self.error,
            "window": self.window,
            "pages": list(self.pages) if self.pages else None,
        }


@dataclass
class ExtractionResult:
    extraction: ContractExtraction
    parse: DocumentParse
    model: str | None = None
    provider: str | None = None
    calls: list[CallTelemetry] = field(default_factory=list)
    needs_review: list[FieldFlag] = field(default_factory=list)
    unresolved_citations: int = 0
    total_citations: int = 0
    invalid_records: list[str] = field(default_factory=list)
    duration_ms: int | None = None

    # --- rollups, all of which end up in front of a person ---
    @property
    def input_tokens(self) -> int:
        return sum(c.input_tokens or 0 for c in self.calls)

    @property
    def output_tokens(self) -> int:
        return sum(c.output_tokens or 0 for c in self.calls)

    @property
    def cache_read_tokens(self) -> int:
        return sum(c.cache_read_tokens or 0 for c in self.calls)

    @property
    def cache_write_tokens(self) -> int:
        return sum(c.cache_write_tokens or 0 for c in self.calls)

    @property
    def cost_usd(self) -> float | None:
        known = [c.cost_usd for c in self.calls if c.cost_usd is not None]
        return round(sum(known), 6) if known else None

    @property
    def uncached_cost_usd(self) -> float | None:
        return uncached_cost_usd(
            self.model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
        )

    @property
    def truncated_calls(self) -> list[str]:
        return [c.call for c in self.calls if c.truncated]

    @property
    def failed_calls(self) -> list[str]:
        return [c.call for c in self.calls if c.error]

    def telemetry(self) -> dict[str, Any]:
        """The run record that is persisted and shown on hover."""
        return {
            "schema_version": SCHEMA_VERSION,
            "model": self.model,
            "provider": self.provider,
            "duration_ms": self.duration_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": self.cost_usd,
            "uncached_cost_usd": self.uncached_cost_usd,
            "records": len(self.extraction.records),
            "counts_by_category": self.extraction.counts_by_category(),
            "citations_resolved": self.total_citations - self.unresolved_citations,
            "citations_total": self.total_citations,
            "invalid_records": len(self.invalid_records),
            "truncated_calls": self.truncated_calls,
            "failed_calls": self.failed_calls,
            "calls": [c.as_dict() for c in self.calls],
        }


def _resolve_citations(citations: list[Citation], parse: DocumentParse) -> tuple[int, int]:
    """Attach the normalised bbox to each citation in place. Returns (resolved, total)."""
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
    for record in extraction.records:
        r, t = _resolve_citations(record.citations, parse)
        resolved += r
        total += t
    return resolved, total


def _collect_flags(extraction: ContractExtraction, threshold: float) -> list[FieldFlag]:
    """What an analyst should look at first.

    Wider than the old rule, which only ever flagged an elected service line. A record with no
    evidence, or a priced line with neither an amount nor a formula, is worth a human's eye
    however confident the model claimed to be.
    """
    flags: list[FieldFlag] = []
    for r in extraction.records:
        label = getattr(r, "item", None) or getattr(r, "program", None) or r.info_type
        if r.confidence <= threshold:
            flags.append(FieldFlag(path=str(label), reason=f"confidence {r.confidence:.2f}"))
        if not r.citations:
            flags.append(FieldFlag(path=str(label), reason="no citation"))
        if r.info_type == "pricing_item" and r.item and not r.included:
            if r.amount is None and not r.calculation and not r.tier_bands:
                flags.append(FieldFlag(path=str(label), reason="priced line with no price"))
    return flags


# What each category writes, per page of contract, measured over the four real runs in dev. Used
# only to decide how many windows a category needs — a rough number is enough, because the fill
# factor leaves room and an overflow is caught and retried.
_OUTPUT_PER_PAGE: dict[str, int] = {
    "misc_operational": 545,
    "definitions": 367,
    "pricing": 236,
    "misc_reference": 156,
    "sla": 122,
    "reporting": 114,
}

# Windowing exists to fit an answer inside a model's ceiling. A category whose whole answer fits
# is asked once, over the whole document, exactly as it always was.
_DEFAULT_OUTPUT_CAP = 64000


@dataclass(frozen=True)
class _Job:
    """One category asked of one window."""

    call: str
    window: Window
    want_doc_meta: bool = False


def _output_cap(provider: LLMProvider) -> int:
    model = getattr(provider, "model_id", None)
    price = price_for(model) if model else None
    return price.max_output_tokens if price else _DEFAULT_OUTPUT_CAP


def _plan_calls(provider: LLMProvider, parse: DocumentParse, wanted: list[str]) -> list[_Job]:
    """Which categories get asked of which slices of the document.

    Each category is windowed on its own. `sla` and `reporting` answer in a few thousand tokens
    for a whole contract and are never split; the responsibilities run to twenty-four thousand
    and are split as many ways as the model's ceiling demands. Sizing them together would cut
    the short categories up for no reason, and every extra boundary is a chance to separate a
    clause from its context.
    """
    cap = _output_cap(provider)
    jobs: list[_Job] = []
    for call in wanted:
        expected = _OUTPUT_PER_PAGE.get(call, 300) * max(1, parse.page_count)
        windows = windows_for_output(parse.pages, expected, cap)
        for window in windows:
            # The parties and the effective date are on page one; only the window holding it is
            # asked, so a later window cannot answer from a cross-reference and overwrite them.
            first = window.index == 0 and call == CACHE_PRIMING_CALL
            jobs.append(_Job(call=call, window=window, want_doc_meta=first))
    if not any(j.want_doc_meta for j in jobs) and jobs:
        jobs[0] = _Job(call=jobs[0].call, window=jobs[0].window, want_doc_meta=True)
    return jobs


def _priming_order(plan: list[_Job]) -> list[_Job]:
    """Put the cache-priming call first; the rest follow and read what it wrote."""
    primer = [j for j in plan if j.call == CACHE_PRIMING_CALL]
    others = [j for j in plan if j.call != CACHE_PRIMING_CALL]
    return primer + others if primer else plan


def _run_call(
    provider: LLMProvider,
    call: str,
    prefix: str,
    doc_type_hint: str | None,
    *,
    window: int = 0,
    pages: tuple[int, int] | None = None,
    want_doc_meta: bool = True,
) -> tuple[CallTelemetry, dict[str, Any] | None]:
    """One category over one window. A failure here costs that, and nothing else."""
    telemetry = CallTelemetry(call=call, window=window, pages=pages)
    try:
        result: LLMResult = provider.call_tool(
            system=SYSTEM_PROMPT,
            cache_prefix=prefix,
            user_text=build_call_instruction(
                call, doc_type_hint=doc_type_hint, want_doc_meta=want_doc_meta
            ),
            tool=CONTRACT_TOOL,
            max_tokens=CALL_BUDGET.get(call, 12000),
        )
    except OutputOverflow as e:
        # Not a failure of the model but of the ask: it ran out of room part-way through the
        # tool call. Named separately so the caller can split the window and try again rather
        # than write the category off.
        telemetry.error = f"OutputOverflow: {e}"
        telemetry.overflowed = True
        log.warning("extraction call %s window %s overflowed its output budget", call, window)
        return telemetry, None
    except Exception as e:  # noqa: BLE001 - one category failing must not lose the others
        telemetry.error = f"{type(e).__name__}: {e}"
        log.warning("extraction call %s failed: %s", call, telemetry.error)
        return telemetry, None

    telemetry.model = result.model
    telemetry.input_tokens = result.input_tokens
    telemetry.output_tokens = result.output_tokens
    telemetry.cache_read_tokens = result.cache_read_tokens
    telemetry.cache_write_tokens = result.cache_write_tokens
    telemetry.stop_reason = result.stop_reason
    telemetry.latency_ms = result.latency_ms
    telemetry.max_output_tokens = result.max_output_tokens
    telemetry.salvaged = result.salvaged
    telemetry.cost_usd = cost_usd(
        result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_write_tokens=result.cache_write_tokens,
    )
    if telemetry.truncated:
        # The tool input is partial JSON. Anything parsed from it is a fragment of the truth,
        # and silence here is what makes truncation dangerous.
        log.warning("extraction call %s hit its output cap; records are incomplete", call)
    return telemetry, result.data or {}


def extract_contract(
    source: Source,
    *,
    doc_type_hint: str | None = None,
    provider: LLMProvider | None = None,
    engine: OCREngine | None = None,
    settings: Settings | None = None,
    parse: DocumentParse | None = None,
    calls: tuple[str, ...] | None = None,
) -> ExtractionResult:
    started = time.monotonic()
    settings = settings or get_settings()
    engine = engine or PyMuPDFEngine()
    # Tables on: the extract pass is the one that needs them, and detection then runs once for
    # the document rather than once here and once in the parse worker.
    parse = parse or engine.parse(source, tables=True)

    if provider is None:
        from ..llm.factory import get_provider

        provider = get_provider(settings)

    wanted = list(calls or CALL_BUDGET.keys())
    plan = _plan_calls(provider, parse, wanted)
    windows = {job.window.index for job in plan}
    if len(windows) > 1:
        log.info(
            "extracting %s pages in %s windows, %s calls",
            parse.page_count, len(windows), len(plan),
        )

    # One prefix per window, built once and shared by every call that reads that window — which
    # is what lets the cache be written once and read by the rest.
    prefixes = {
        job.window.index: build_document_prefix(job.window.pages, of_pages=parse.page_count)
        for job in plan
    }

    telemetry: list[CallTelemetry] = []
    payloads: list[tuple[CallTelemetry, dict[str, Any]]] = []

    def run(job: _Job) -> tuple[CallTelemetry, dict[str, Any] | None]:
        return _run_call(
            provider, job.call, prefixes[job.window.index], doc_type_hint,
            window=job.window.index,
            pages=(job.window.first_page, job.window.last_page),
            want_doc_meta=job.want_doc_meta,
        )

    # With one window there is one cached prefix, so a single call is run first to write it and
    # the rest read it. With many, every window is its own entry and there is nothing to prime:
    # a serial first call would just be a slower start.
    ordered = _priming_order(plan) if len(windows) == 1 else plan
    if len(windows) == 1:
        first, rest = ordered[0], ordered[1:]
        t, data = run(first)
        telemetry.append(t)
        if data is not None:
            payloads.append((t, data))
    else:
        first, rest = None, ordered

    if rest:
        workers = max(1, min(settings.extract_max_concurrency, len(rest)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for t, data in pool.map(run, rest):
                telemetry.append(t)
                if data is not None:
                    payloads.append((t, data))

    raw_records: list[dict[str, Any]] = []
    invalid: list[str] = []
    doc_meta = DocumentMeta()
    for tel, payload in payloads:
        good, bad = parse_records(payload.get("records") or [])
        tel.records = len(good)
        raw_records.extend(r.model_dump(mode="json") for r in good)
        invalid.extend(bad)
        raw_meta = payload.get("doc_meta")
        if raw_meta:
            try:
                merged = {**doc_meta.model_dump(exclude_none=True), **{
                    k: v for k, v in raw_meta.items() if v is not None
                }}
                doc_meta = DocumentMeta.model_validate(merged)
            except Exception as e:  # noqa: BLE001 - meta is a bonus, records are the point
                invalid.append(f"doc_meta: {type(e).__name__}: {e}")

    # Overlapping windows read some terms twice. Folding them back here rather than downstream
    # keeps the stored extraction, the record counts and the telemetry describing one document
    # rather than the sum of its windows.
    merged_records, merge_report = merge_records(raw_records)
    records, still_bad = parse_records(merged_records)
    invalid.extend(still_bad)

    extraction = ContractExtraction(doc_meta=doc_meta, records=records)
    resolved, total = resolve_all_citations(extraction, parse)
    flags = _collect_flags(extraction, settings.review_confidence_threshold)

    # Whichever model actually answered. The fallback provider can serve from its secondary,
    # so asking the provider object would report the wrong one on exactly the runs that matter.
    model = next((c.model for c in telemetry if c.model), None)

    return ExtractionResult(
        extraction=extraction,
        parse=parse,
        model=model,
        provider=getattr(provider, "name", None),
        calls=telemetry,
        needs_review=flags,
        unresolved_citations=total - resolved,
        total_citations=total,
        invalid_records=invalid,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
