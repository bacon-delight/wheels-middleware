"""Turn an extraction into stored rows, without destroying what an analyst has already done.

Two problems this solves that the previous design had.

**Identity.** Row ids used to be freshly generated on every run, and the id is part of the sort
key, so re-extracting a document appended a second copy of every term rather than replacing it.
At thirteen rows that was easy to miss; at three hundred it is not. Ids are now a hash of the
content that identifies a term, so the same term in a later run is the same row.

**Merge, not overwrite.** An analyst approves and corrects terms. Re-running extraction must not
silently throw that away, nor silently keep an approval against a value that has since changed.
So an unchanged term is left completely alone, a changed term is rewritten and flagged as
changed since approval, and a term the new run no longer finds is marked superseded rather than
deleted.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..catalog.models import CatalogSnapshot
from ..catalog.resolve import resolve as resolve_service
from ..ocr.citations import _norm as norm_text
from ..store.models import EngagementService, TermRow
from .schema import CATEGORY_OF

# Fields that identify a term, per record type. Deliberately not every field: a term whose
# confidence or wording shifts slightly is the same term, and should keep its approval history.
_IDENTITY: dict[str, tuple[str, ...]] = {
    "pricing_item": ("program", "item", "sub_category", "frequency"),
    "sla_item": ("category", "contract_sub_section"),
    "reporting_requirement": ("report_name",),
    "definition": ("term",),
    "online_tool": ("tool_name", "platform"),
    "responsibility": ("topic", "task"),
    "signature": ("company", "name"),
    "information_section": ("topic",),
    "uncategorised": ("item",),
}

# What the analyst sees in a list, per record type: the headline and the line beneath it.
_TITLE: dict[str, tuple[str, str | None]] = {
    "pricing_item": ("item", "program"),
    "sla_item": ("category", "contract_sub_section"),
    "reporting_requirement": ("report_name", "frequency"),
    "definition": ("term", None),
    "online_tool": ("tool_name", "platform"),
    "responsibility": ("task", "responsible_party"),
    "signature": ("name", "company"),
    "information_section": ("topic", None),
    "uncategorised": ("item", None),
}


def record_id_for(record: dict[str, Any]) -> str:
    """A stable id for a term.

    Normalised so that whitespace and punctuation drift between two runs does not mint a new
    row, but a changed amount does — a fee that moves from $15 to $20 is a different term and
    must lose its approval.
    """
    info_type = record.get("info_type", "")
    parts = [info_type]
    for field in _IDENTITY.get(info_type, ("title",)):
        parts.append(norm_text(str(record.get(field) or "")))
    if info_type == "pricing_item":
        parts.append(repr(record.get("amount")))
        parts.append(repr(record.get("included")))
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def _body_hash(record: dict[str, Any]) -> str:
    """Everything that would change what a person reads, minus what the server owns."""
    ignore = {"program_id", "item_id", "catalog_match", "confidence"}
    payload = {k: v for k, v in sorted(record.items()) if k not in ignore}
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _titles(record: dict[str, Any]) -> tuple[str, str | None]:
    title_field, subtitle_field = _TITLE.get(record.get("info_type", ""), ("title", None))
    title = record.get(title_field)
    if not title and record.get("info_type") == "pricing_item":
        # A program named with nothing priced under it is a real record, and its program is
        # the only thing there is to call it.
        title = record.get("program")
    subtitle = record.get(subtitle_field) if subtitle_field else None
    title = str(title or record.get("info_type") or "Untitled")[:300]
    subtitle = str(subtitle)[:300] if subtitle else None
    # A row that repeats itself tells the reader nothing twice. This happens on every pricing
    # record that names a program without pricing anything under it: both the headline and the
    # line beneath it fall back to the program.
    if subtitle and subtitle.strip().casefold() == title.strip().casefold():
        subtitle = None
    return title, subtitle


def needs_review_for(record: dict[str, Any], catalog_match: str | None, threshold: float) -> bool:
    """What an analyst should be shown first.

    Wider than confidence alone. A term with no evidence cannot be checked against the page; a
    priced line with no price is incomplete on its face; and a program the catalog could not
    confidently place is the one case where a person's judgement adds the most.
    """
    if float(record.get("confidence") or 0.0) <= threshold:
        return True
    if not record.get("citations"):
        return True
    if record.get("info_type") == "pricing_item":
        if catalog_match in ("fuzzy", "unmatched"):
            return True
        priced = record.get("item") and not record.get("included")
        if priced and record.get("amount") is None and not record.get("calculation"):
            return not record.get("tier_bands")
    return False


def build_rows(
    engagement_id: str,
    document_id: str,
    version: int,
    records: list[dict[str, Any]],
    *,
    catalog: CatalogSnapshot | None = None,
    threshold: float = 0.8,
    now: str | None = None,
) -> tuple[list[TermRow], list[str]]:
    """Rows for one document version, plus the program names the catalog could not place."""
    rows: list[TermRow] = []
    unmatched: list[str] = []
    seen: set[str] = set()

    for record in records:
        info_type = record.get("info_type") or ""
        category = CATEGORY_OF.get(info_type)
        if category is None:
            continue

        program_id = item_id = match = None
        if info_type == "pricing_item" and catalog is not None:
            found = resolve_service(catalog, record.get("program"), record.get("item"))
            program_id, item_id, match = found.program_id, found.item_id, found.kind
            if found.program is None and record.get("program"):
                unmatched.append(str(record["program"]))
            record = {**record, "program_id": program_id, "item_id": item_id,
                      "catalog_match": match}

        rid = record_id_for(record)
        if rid in seen:
            # Two records that identify the same term really are one term.
            continue
        seen.add(rid)

        title, subtitle = _titles(record)
        basis = billing_class = None
        if info_type == "pricing_item":
            from ..billing.frequency import classify

            found = classify(record.get("frequency"), record.get("calculation"))
            basis, billing_class = found.unit_basis, found.billing_class
        rows.append(
            TermRow(
                engagement_id=engagement_id,
                document_id=document_id,
                version=version,
                record_id=rid,
                category=category,
                info_type=info_type,
                title=title,
                subtitle=subtitle,
                amount=record.get("amount"),
                frequency=record.get("frequency"),
                unit_basis=basis,
                billing_class=billing_class,
                program_id=program_id,
                catalog_item_id=item_id,
                catalog_match=match,
                confidence=float(record.get("confidence") or 0.0),
                needs_review=needs_review_for(record, match, threshold),
                record=record,
                citations=record.get("citations") or [],
                notes=record.get("notes"),
                created_at=now,
                updated_at=now,
            )
        )
    return rows, unmatched


def merge_rows(
    existing: list[TermRow], fresh: list[TermRow]
) -> tuple[list[TermRow], list[TermRow]]:
    """Decide what to write and what to mark superseded.

    Returns (to_write, to_supersede). A row whose body has not changed is in neither list: not
    writing it is what preserves the analyst's approval, their correction and their notes.
    """
    by_id = {r.record_id: r for r in existing}
    write: list[TermRow] = []
    for row in fresh:
        prior = by_id.pop(row.record_id, None)
        if prior is None:
            write.append(row)
            continue
        if _body_hash(prior.record) == _body_hash(row.record):
            if prior.superseded:
                # It came back. Un-supersede it, keeping the review state it had.
                prior.superseded = False
                write.append(prior)
            continue
        # The term moved. Keep the row, lose the approval, and say why.
        row.approved = False
        row.needs_review = True
        row.changed_since_approval = prior.approved
        row.corrected = prior.corrected
        row.notes = prior.notes or row.notes
        row.created_at = prior.created_at or row.created_at
        write.append(row)

    supersede = [r for r in by_id.values() if not r.superseded]
    for row in supersede:
        row.superseded = True
    return write, supersede


def coverage_rows(
    engagement_id: str,
    document_id: str,
    version: int,
    rows: list[TermRow],
    catalog: CatalogSnapshot,
    *,
    now: str | None = None,
) -> list[EngagementService]:
    """Which catalog programs this document version shows the engagement avails.

    A program counts when a pricing record resolves to it, whether or not that record carries a
    price: a contract that names a program and charges nothing for it is still a service the
    customer receives, and that is exactly the case the human extraction recorded twelve times.
    """
    names = {p.program_id: p.name for p in catalog.programs}
    by_program: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.category != "pricing" or not row.program_id or row.superseded:
            continue
        entry = by_program.setdefault(
            row.program_id, {"items": set(), "priced": 0}
        )
        if row.catalog_item_id:
            entry["items"].add(row.catalog_item_id)
        if row.amount is not None or row.record.get("calculation"):
            entry["priced"] += 1

    return [
        EngagementService(
            engagement_id=engagement_id,
            program_id=program_id,
            program_name=names.get(program_id, program_id),
            document_id=document_id,
            version=version,
            item_ids=sorted(entry["items"]),
            priced_item_count=entry["priced"],
            created_at=now,
        )
        for program_id, entry in sorted(by_program.items())
    ]
