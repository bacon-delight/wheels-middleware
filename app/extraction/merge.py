"""Fold the answers from many windows back into one reading of the contract.

Asking six questions of ten windows produces sixty answers about one document, and the same term
appears in as many of them as the windows that saw it. Collapsing those is mostly free: a term's
id is a hash of what it says, so two readings of one definition are already one row.

What is not free is everything the hash was never meant to decide.

- **A tiered fee split across a boundary.** Window A reports bands 1-250, window B reports 251+.
  Program, item, frequency and amount are identical, so both hash to the same id — and a merge
  that keeps the first and discards the rest would delete half a price schedule. List fields are
  therefore unioned, never replaced.

- **The same line read twice, once without its price.** Window A sees the fee table and reports
  $15.00; window B sees only the sentence that mentions the fee and reports no amount. The
  amount is part of a pricing term's identity, so these are two ids and two rows — one of which
  is a fee that does not exist. Neither is silently preferred: both are kept and the
  disagreement is reported, because the alternative is inventing an answer.

- **A program named in one window and priced in another.** The pricing prompt asks for a bare
  record when a contract names a program it charges nothing for. A window that sees the name but
  not the schedule will duly produce one, and downstream that becomes an unpriced enrolment
  sitting beside the priced reality. Bare records are dropped when the same program is priced
  anywhere in the document.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..ocr.citations import _norm as norm_text
from .materialize import record_id_for

# Fields whose value is a list of things found in the document rather than a single reading.
# Two windows can each hold part of the truth, so these are combined rather than chosen between.
_UNION_FIELDS = ("tier_bands", "conditions", "applicable_programs", "citations")

# What makes two pricing records "the same line" for the purpose of spotting a disagreement
# about money. Deliberately excludes the amount, which is the thing being compared.
_LINE = ("program", "item", "sub_category", "frequency")


@dataclass
class MergeReport:
    """What the merge did, for telemetry and for a person deciding whether to trust it."""

    kept: int = 0
    collapsed: int = 0          # duplicates folded into an existing record
    unioned: int = 0            # records that gained list entries from another window
    bare_dropped: int = 0       # "named but unpriced" records that were priced elsewhere
    amount_conflicts: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.amount_conflicts is None:
            self.amount_conflicts = []

    def as_dict(self) -> dict[str, Any]:
        return {
            "kept": self.kept,
            "collapsed": self.collapsed,
            "unioned": self.unioned,
            "bare_dropped": self.bare_dropped,
            "amount_conflicts": self.amount_conflicts,
        }


def _completeness(record: dict[str, Any]) -> int:
    return sum(1 for v in record.values() if v not in (None, "", [], {}))


def _entry_key(value: Any) -> str:
    if isinstance(value, dict):
        return "|".join(f"{k}={value[k]!r}" for k in sorted(value))
    return repr(value)


def _union_lists(winner: dict[str, Any], other: dict[str, Any]) -> bool:
    """Add anything `other` holds that `winner` does not. True if anything was added."""
    changed = False
    for field in _UNION_FIELDS:
        extra = other.get(field)
        if not isinstance(extra, list) or not extra:
            continue
        have = winner.get(field)
        if not isinstance(have, list):
            winner[field] = list(extra)
            changed = True
            continue
        seen = {_entry_key(x) for x in have}
        for entry in extra:
            if _entry_key(entry) not in seen:
                have.append(entry)
                seen.add(_entry_key(entry))
                changed = True
    return changed


def _fill_gaps(winner: dict[str, Any], other: dict[str, Any]) -> None:
    """Take what the winner is missing from a record that has it."""
    for key, value in other.items():
        if key in _UNION_FIELDS:
            continue
        if winner.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
            winner[key] = value


def _line_key(record: dict[str, Any]) -> str:
    return "|".join(norm_text(str(record.get(f) or "")) for f in _LINE)


def merge_records(
    records: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], MergeReport]:
    """One reading of the contract from many overlapping ones."""
    report = MergeReport()
    by_id: dict[str, dict[str, Any]] = {}

    for record in records:
        rid = record_id_for(record)
        existing = by_id.get(rid)
        if existing is None:
            by_id[rid] = dict(record)
            continue
        report.collapsed += 1
        # Keep whichever reading says more, then take everything the other one knew.
        if _completeness(record) > _completeness(existing):
            richer, poorer = dict(record), existing
            by_id[rid] = richer
        else:
            richer, poorer = existing, record
        if _union_lists(richer, poorer):
            report.unioned += 1
        _fill_gaps(richer, poorer)

    kept = list(by_id.values())

    # A program that is priced somewhere is not an unpriced enrolment anywhere.
    priced_programs = {
        norm_text(str(r.get("program") or ""))
        for r in kept
        if r.get("info_type") == "pricing_item" and r.get("item")
    }
    survivors = []
    for record in kept:
        bare = (
            record.get("info_type") == "pricing_item"
            and not record.get("item")
            and norm_text(str(record.get("program") or "")) in priced_programs
        )
        if bare:
            report.bare_dropped += 1
            continue
        survivors.append(record)

    # Two windows reading the same line and disagreeing about the money. Both are kept — one of
    # them is wrong, and which one is not ours to guess — but the disagreement is named.
    lines: dict[str, list[dict[str, Any]]] = {}
    for record in survivors:
        if record.get("info_type") != "pricing_item":
            continue
        lines.setdefault(_line_key(record), []).append(record)
    for key, group in lines.items():
        amounts = {(_entry_key(r.get("amount")), _entry_key(r.get("included"))) for r in group}
        if len(amounts) > 1:
            shown = ", ".join(sorted(str(r.get("amount")) for r in group))
            report.amount_conflicts.append(f"{key.replace('|', ' / ')}: {shown}")

    report.kept = len(survivors)
    return survivors, report
