"""Generate the billing configuration from a submission's approved terms.

This is the "no manual keying" hand-off: the elected service lines (and MLA lease terms) are
aggregated into a machine-readable config and written to S3. A real billing-system integration
would consume this artifact.
"""

from __future__ import annotations

import datetime
from typing import Any

from ..objects import billing_config_key
from ..store.models import Engagement, Payment
from ..store.repository import Repository, utcnow
from .schedule import generate_rows, normalize_frequency


def _fee_item(record: dict[str, Any]) -> dict[str, Any] | None:
    """Project a pricing term into the fee-item shape the estimate consumes.

    The unit basis is derived from the contract's own frequency wording rather than trusted
    from the model, because that one field decides whether a charge enters recurring dues.
    """
    from .frequency import classify

    found = classify(record.get("frequency"), record.get("calculation"))
    amount = record.get("amount")
    tiers = record.get("tier_bands") or []
    if amount is None and not tiers and record.get("rate_pct") is None:
        return None  # a program named with nothing priced under it bills nothing
    return {
        "description": record.get("item") or record.get("program") or "",
        "fee_type": record.get("fee_type") or "other",
        "amount": amount,
        "currency": record.get("currency") or "USD",
        "rate_pct": record.get("rate_pct"),
        "unit_basis": found.unit_basis,
        "billing_class": found.billing_class,
        "minimum": record.get("minimum"),
        "maximum": record.get("maximum"),
        "tier_bands": tiers,
        "conditions": record.get("conditions") or [],
    }


def build_billing_config(
    repo: Repository, engagement_id: str, submission_id: str, s3=None
) -> dict[str, Any]:
    sub = repo.get_submission(engagement_id, submission_id)
    engagement = repo.get_engagement(engagement_id)
    config: dict[str, Any] = {
        "engagement_id": engagement_id,
        "submission_id": submission_id,
        "client_name": engagement.client_name if engagement else None,
        "generated_at": utcnow(),
        "service_lines": [],
        "pricing_items": [],
        "billing_frequency": None,
        "excluded_services": [],
    }
    if sub is None:
        return config

    # Pricing terms are grouped by the program they belong to, and projected into the same
    # `service_lines` shape the estimate and the interface already read. Keeping that shape is
    # what lets the extraction change underneath billing without billing changing at all.
    by_program: dict[str, dict[str, Any]] = {}
    excluded: list[str] = []
    for document_id in sub.docs().values():
        doc = repo.get_document(engagement_id, document_id)
        if doc is None:
            continue
        version = repo.get_document_version(engagement_id, document_id, doc.current_version)
        if version and version.extraction:
            meta = version.extraction.get("doc_meta") or {}
            config["billing_frequency"] = (
                config.get("billing_frequency") or meta.get("billing_frequency")
            )
        for term in repo.list_terms(engagement_id, document_id, doc.current_version, "pricing"):
            if term.superseded:
                continue
            record = term.record or {}
            fee = _fee_item(record)
            entry = by_program.setdefault(
                term.program_id or (record.get("program") or "Unclassified"),
                {
                    "service": record.get("program") or "Unclassified",
                    "program_id": term.program_id,
                    "fee_items": [],
                    "approved": True,
                },
            )
            if fee is not None:
                entry["fee_items"].append(fee)
                # Each billed line keeps a way back to the clause it was read from. Without it
                # the config is a list of numbers nobody can check; with it the audit can put
                # the fee beside the sentence that set it.
                config["pricing_items"].append(
                    {**fee, "program": entry["service"], "item": record.get("item"),
                     "approved": term.approved, "billing_class": fee["billing_class"],
                     "record_id": term.record_id, "document_id": document_id,
                     "version": doc.current_version, "doc_type": doc.doc_type,
                     "frequency": record.get("frequency"),
                     "calculation": record.get("calculation"),
                     "confidence": term.confidence,
                     "citations": term.citations or []}
                )
            entry["approved"] = entry["approved"] and term.approved

    config["service_lines"] = list(by_program.values())
    # What the catalog says Wheels sells that this engagement's agreements do not price. More
    # honest than the old list of un-elected enum members, which could only ever name thirteen.
    covered = {c.program_id for c in repo.list_coverage(engagement_id)}
    try:
        from ..catalog.store import load_snapshot

        snapshot = load_snapshot(repo)
        excluded = [p.name for p in snapshot.active_programs if p.program_id not in covered]
    except Exception:  # noqa: BLE001 - a missing catalog must not stop billing
        excluded = []
    config["excluded_services"] = excluded

    if s3 is None:
        from ..store.s3 import S3Store

        s3 = S3Store()
    s3.put_json(billing_config_key(engagement_id, submission_id), config)
    return config


def _billing_start(engagement: Engagement | None, config: dict[str, Any] | None,
                   today: datetime.date) -> datetime.date:
    if engagement and engagement.billing_start:
        return datetime.date.fromisoformat(engagement.billing_start[:10])
    candidates = [(config or {}).get("generated_at")]
    if engagement:
        candidates.append(engagement.created_at)
    for candidate in candidates:
        if candidate:
            try:
                return datetime.datetime.fromisoformat(candidate).date()
            except ValueError:
                continue
    return today


def ensure_schedule(
    repo: Repository,
    engagement: Engagement,
    submission_id: str,
    config: dict[str, Any] | None,
    today: datetime.date | None = None,
) -> list[Payment]:
    """Generate + persist the payment schedule once; idempotent (returns existing if present)."""
    existing = repo.list_payments(engagement.engagement_id)
    if existing:
        return sorted(existing, key=lambda p: p.seq)
    today = today or datetime.date.today()
    start = _billing_start(engagement, config, today)
    frequency = normalize_frequency(config)
    payments = []
    for row in generate_rows(start, frequency):
        payments.append(
            repo.put_payment(
                Payment(engagement_id=engagement.engagement_id, submission_id=submission_id, **row)
            )
        )
    repo.set_engagement_schedule(engagement.engagement_id, start.isoformat(), frequency)
    return payments
