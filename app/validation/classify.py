"""Work out what an uploaded agreement is, and whether it is the one in force.

Customers hand over whatever they have: usually the one or two agreements that govern the
engagement today, occasionally a stack that includes superseded ones kept for the record. They
should not have to tell us which is which, so we read it off the document.

Classification is deliberately text-based rather than a model call. The distinction between a
Master Lease Agreement and a Master Service Agreement is carried in the title and repeated
throughout, so matching is reliable and costs nothing; an unreadable document falls back to
UNKNOWN rather than guessing.
"""

from __future__ import annotations

import datetime
import re

UNKNOWN = "UNKNOWN"
MLA = "MLA"
MSA = "MSA"

_LEASE = re.compile(r"master\s+lease\s+agreement", re.I)
_SERVICE = re.compile(r"master\s+(service|services)\s+agreement", re.I)

# "effective as of January 1, 2024", "dated as of 1 January 2024", "Effective Date: 2024-01-01"
_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
)
_DATE_PATTERNS = [
    re.compile(
        rf"(?:effective|dated)\s+(?:date\s*[:\-]?\s*|as\s+of\s+)?"
        rf"(?P<month>{_MONTHS})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<year>\d{{4}})",
        re.I,
    ),
    re.compile(
        rf"(?:effective|dated)\s+(?:date\s*[:\-]?\s*|as\s+of\s+)?"
        rf"(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{_MONTHS}),?\s+(?P<year>\d{{4}})",
        re.I,
    ),
    re.compile(
        r"(?:effective|dated)\s+(?:date\s*[:\-]?\s*|as\s+of\s+)?"
        r"(?P<year>\d{4})-(?P<mon>\d{2})-(?P<dayn>\d{2})",
        re.I,
    ),
]
_MONTH_INDEX = {
    m: i + 1
    for i, m in enumerate(
        "january february march april may june july august september october "
        "november december".split()
    )
}


def classify_doc_type(full_text: str) -> tuple[str, float]:
    """Return the agreement type and how confident the match is.

    Both phrases can appear in one document — an MLA routinely cross-references the MSA — so
    the decision is by weight of mentions, not mere presence.
    """
    lease = len(_LEASE.findall(full_text or ""))
    service = len(_SERVICE.findall(full_text or ""))
    if not lease and not service:
        return UNKNOWN, 0.0
    if lease == service:
        # A tie means both are discussed as much as each other, so fall back to whichever the
        # document leads with — the title comes before any cross-reference to the other.
        first_lease = _LEASE.search(full_text)
        first_service = _SERVICE.search(full_text)
        if first_lease.start() < first_service.start():
            return MLA, 0.6
        return MSA, 0.6
    winner, top, other = (MLA, lease, service) if lease > service else (MSA, service, lease)
    return winner, round(min(0.99, 0.6 + 0.4 * (top - other) / top), 2)


def find_effective_date(full_text: str) -> str | None:
    """The date the agreement took effect, used to order one agreement against another."""
    text = (full_text or "")[:20000]
    for pattern in _DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        parts = m.groupdict()
        try:
            if parts.get("mon"):
                y, mo, d = int(parts["year"]), int(parts["mon"]), int(parts["dayn"])
            else:
                y = int(parts["year"])
                mo = _MONTH_INDEX[parts["month"].lower()]
                d = int(parts["day"])
            if 1900 < y < 2200:
                return datetime.date(y, mo, d).isoformat()
        except (ValueError, KeyError):
            continue
    return None


def rank_standing(documents: list[dict]) -> dict[str, str]:
    """Decide which agreement of each type is in force.

    Newest effective date wins; documents without one fall back to upload order, which is the
    best available proxy. Returns document_id -> CURRENT | SUPERSEDED.
    """
    by_type: dict[str, list[dict]] = {}
    for d in documents:
        by_type.setdefault(d.get("doc_type") or UNKNOWN, []).append(d)

    out: dict[str, str] = {}
    for doc_type, docs in by_type.items():
        if doc_type == UNKNOWN:
            # An unclassified document governs nothing until we know what it is.
            for d in docs:
                out[d["document_id"]] = "SUPERSEDED" if len(docs) > 1 else "CURRENT"
            continue
        ordered = sorted(
            docs,
            key=lambda d: (d.get("effective_date") or "", d.get("created_at") or ""),
            reverse=True,
        )
        for i, d in enumerate(ordered):
            out[d["document_id"]] = "CURRENT" if i == 0 else "SUPERSEDED"
    return out


def derive_scope(current_types: set[str]) -> str | None:
    """What the engagement covers, read off the agreements actually in force."""
    has_lease, has_service = MLA in current_types, MSA in current_types
    if has_lease and has_service:
        return "LEASE_AND_SERVICE"
    if has_lease:
        return "LEASE_ONLY"
    if has_service:
        return "SERVICE_ONLY"
    return None
