"""Seed a priced term the way extraction would, for tests that drive billing.

Billing reads stored terms now rather than per-service-line review fields, so tests that need
an engagement to have a price must write the shape the extract worker writes.
"""

from __future__ import annotations

from typing import Any

from app.extraction.materialize import build_rows
from app.store.repository import Repository, utcnow


def seed_pricing(
    repo: Repository,
    engagement_id: str,
    document_id: str,
    version: int = 1,
    *,
    program: str = "Fuel Management Program",
    item: str = "Monthly Program Fee",
    amount: float = 4.0,
    frequency: str = "pvpm",
    approved: bool = True,
    confidence: float = 0.95,
    extra: dict[str, Any] | None = None,
) -> None:
    record = {
        "info_type": "pricing_item",
        "program": program,
        "item": item,
        "amount": amount,
        "frequency": frequency,
        "confidence": confidence,
        "citations": [{"page": 1, "quote": f"{item} {amount}"}],
        **(extra or {}),
    }
    rows, _ = build_rows(engagement_id, document_id, version, [record], now=utcnow())
    for row in rows:
        row.approved = approved
        row.needs_review = False
    repo.put_terms(rows)
