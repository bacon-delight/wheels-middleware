"""Prompts for categorised contract extraction.

One document, five calls. A single call cannot hold the output: a hand extraction of the
Walmart statement of work runs to 158 records, and the previous single-call design capped out
at 8,000 output tokens and truncated mid-tool-use with no error.

The five calls share one **cacheable prefix** — the tool schema, the system prompt and the whole
document text, in that order, because that is the order a request renders in. Only the short
instruction after the cache breakpoint differs, naming which record types that call may return.
Anything that varies per request must live after the breakpoint or the cache is rebuilt every
time, which is silent and costs four fifths of the saving.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..ocr.base import DocumentParse, PageParse
from .schema import CALL_RECORD_TYPES, INFO_TYPE_LABELS

SYSTEM_PROMPT = """You are a fleet-contract analyst. You read one Wheels contract — a master \
service agreement, a master lease agreement, a statement of work, a professional services \
agreement, or an amendment — and record what it says into the provided tool.

You extract into four categories:
- PRICING: every priced line, and every program the contract names even when it prices nothing \
under it.
- SLA: service level standards, and the fee credits owed when they are missed.
- REPORTING: reports the vendor owes the client.
- MISC: definitions, online tools, who is responsible for what, signatures, and any other \
section worth keeping.

Rules that matter:

- COPY, DO NOT PARAPHRASE. `program`, `item`, `frequency` and every other text field take the \
contract's own words. Program names are matched against a catalog downstream, so \
"Fuel Management Program" must not become "Fuel Program" or "fuel management".

- A PROGRAM WITH NO PRICED ITEM IS STILL A RECORD. When a contract names a program but charges \
nothing separately for it, emit a pricing item with the program and no `item`. That is how we \
know the client is enrolled. Never invent an item name to fill the gap.

- FREQUENCY COMES FROM THE COLUMN. In a pricing table, the column an amount sits under IS its \
frequency: a value under "PER VEHICLE PER MONTH ("PVPM") FEES" is `pvpm`; the same value under \
"OTHER FEES" or "PER OCCURRENCE FEES" is not. Read the column header, and copy the frequency \
text as the contract writes it ("pvpm", "per card", "per driver per month", "per issuance").

- "Included" IS A PRICE. When a contract says a fee is Included or bundled at no charge, set \
`included: true` rather than an amount of 0, unless it literally says $0.00.

- CITE EVERYTHING, BRIEFLY. Every record carries exactly one citation. Its `quote` must be \
copied CHARACTER-FOR-CHARACTER from the page, but it should be SHORT: the most distinctive \
five to fifteen words of the sentence, enough to find it and no more. A quote is used to locate \
the text on the page, not to reproduce it, so quoting a whole clause wastes the response budget \
and risks the record being cut off entirely. Never paraphrase — a paraphrase finds nothing. \
Give the 1-based `page` and a `section_label` such as "Schedule 2" or "Section 2(a)". Do not \
output `bbox`, `char_span`, `program_id`, `item_id` or `catalog_match`; those are computed \
server-side.

- BE CONCISE IN EVERY FIELD. Copy names and amounts exactly, but do not pad descriptions. One \
record that is complete beats two that are elaborate.

- LEASE MECHANICS ARE PRICING. Per-class depreciation rates, lease administrative fees and the \
lease rate formula are pricing items under the lease program, with the vehicle class in \
`sub_category` and the rate in `rate_pct` / `rate_index` / `spread_bps`. Early termination \
formulas and TRAC surplus splits are `information_section` records.

- `records` IS A JSON ARRAY, not a string containing one. Emit it as a real array of objects. \
Escape any quotation mark that appears inside a value.

- NEVER INVENT A NUMBER. If a value is absent or ambiguous, leave it out and lower that \
record's `confidence`. Reserve confidence >= 0.9 for values copied directly from an explicit \
table. Extraction that is confidently wrong is worse than extraction that is honestly unsure.
"""


def _table_block(page_number: int, index: int, rows: list[list[str]]) -> str:
    """Render one detected table.

    Empty cells are preserved as empty columns on purpose. In a pricing schedule the column an
    amount lands in is what distinguishes a recurring per-vehicle fee from a one-off charge, so
    collapsing the blanks would destroy the single most useful thing a table tells us.
    """
    lines = [f"----- PAGE {page_number} TABLE {index} ({len(rows)} rows) -----"]
    for row in rows:
        cells = [(c or "").replace("\n", " ").strip() for c in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def build_document_prefix(
    parse: DocumentParse | Sequence[PageParse], *, of_pages: int | None = None
) -> str:
    """The cacheable half: the document, identical for every call that shares it.

    Nothing request-specific belongs here. A document-type hint, a category name or a timestamp
    in this string would invalidate the cache on every call while looking entirely harmless.

    Takes either a whole parse or a slice of its pages, so one window of a long contract renders
    the same way the whole of a short one does. Page numbers are the document's own throughout —
    never renumbered per window — because a citation resolves by the number in its marker.
    """
    pages = parse.pages if isinstance(parse, DocumentParse) else list(parse)
    parts = [
        "The contract follows, delimited by page markers. Cite the page number shown in the "
        "marker where each value appears. Where a page has detected tables, they are repeated "
        "after the page text in a pipe-delimited form; empty columns are significant.\n"
    ]
    if of_pages is not None and pages and len(pages) < of_pages:
        # Said once, here, rather than in each call's instruction: it belongs to the window and
        # is identical across the calls that share it, so it stays inside the cached prefix.
        parts.append(
            f"These are pages {pages[0].page_number}-{pages[-1].page_number} of a "
            f"{of_pages}-page contract. Extract only what these pages state. Do not infer what "
            "other pages contain, and do not remark on anything being missing."
        )
    for page in pages:
        parts.append(f"===== PAGE {page.page_number} =====\n{page.text}")
        for i, table in enumerate(getattr(page, "tables", []) or [], start=1):
            parts.append(_table_block(page.page_number, i, table.rows))
    return "\n\n".join(parts)


def _type_list(call: str) -> str:
    return ", ".join(
        f"`{t}` ({INFO_TYPE_LABELS.get(t, t)})" for t in CALL_RECORD_TYPES[call]
    )


_CALL_FOCUS: dict[str, str] = {
    "pricing": (
        "Work through every pricing schedule, fee table, amendment and priced clause in the "
        "document. Emit one record per priced line, splitting a fee that differs by vehicle "
        "class into one record per class with the class in `sub_category`. Then go back through "
        "the document for programs that are named but priced nowhere, and emit a record for "
        "each with the program name and no `item`.\n\n"
        "Also fill `doc_meta` on this call only: the document type, the client's name, the "
        "effective date if the document states one, payment terms, and the billing frequency."
    ),
    "sla": (
        "Emit one record per service level standard, and one per fee credit or remedy owed when "
        "a standard is missed. A remedy record has a `category` and a `calculation` but no "
        "`service_level_standard`, and that is correct — do not invent a standard for it."
    ),
    "reporting": (
        "Emit one record per report the vendor owes. `applicable_programs` is a list: a report "
        "that serves two programs names both."
    ),
    "definitions": (
        "Emit one record per term the contract formally defines — every phrase introduced in "
        "quotation marks followed by 'means', and every term the document says has the meaning "
        "given elsewhere. Contracts define dozens of these; work through the document from "
        "start to finish and do not stop early. Copy the definition text as written."
    ),
    "misc_reference": (
        "Emit every online tool or platform the contract describes, every signature block, and "
        "any section that states a fact about the agreement without imposing a duty.\n\n"
        "A signature record is an execution block naming a person who signed: a company, a "
        "name and a title. The same block reproduced in a page footer or a document-tracking "
        "stamp is not a separate signature — emit each distinct signatory once."
    ),
    "misc_operational": (
        "Emit every obligation the contract places on either party, naming the task, who owes "
        "it, and when. Use `uncategorised` for a section worth keeping that is none of the "
        "above — payment terms, insurance requirements, auditability, penalties."
    ),
}


def build_call_instruction(call: str, doc_type_hint: str | None = None) -> str:
    """The varying half, sent after the cache breakpoint.

    The document-type hint lives here rather than in the prefix. It used to sit in the header
    above the document, where it would have silently rebuilt the cache on every request.
    """
    if call not in CALL_RECORD_TYPES:
        raise ValueError(f"unknown extraction call: {call!r}")
    lines = [
        f"Extract ONLY these record types from the contract above: {_type_list(call)}.",
        "Emit nothing of any other type; the other types are covered by separate passes.",
        "",
        _CALL_FOCUS[call],
        "",
        "Be exhaustive. It is far worse to miss a record than to include an uncertain one with "
        "a low confidence.",
    ]
    if doc_type_hint:
        lines.append(f"\nThe document type is believed to be: {doc_type_hint}.")
    return "\n".join(lines)
