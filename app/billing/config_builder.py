"""Generate the billing configuration from a submission's approved terms.

This is the "no manual keying" hand-off: the elected service lines (and MLA lease terms) are
aggregated into a machine-readable config and written to S3. A real billing-system integration
would consume this artifact.
"""

from __future__ import annotations

import datetime
from typing import Any

from ..extraction.schema import UnitBasis
from ..objects import billing_config_key
from ..store.models import Engagement, Payment
from ..store.repository import Repository, utcnow
from .frequency import CREDIT, ONE_TIME, PER_DRIVER, RECURRING, USAGE
from .schedule import generate_rows, normalize_frequency

# What a charge bills on, once somebody has said what kind of charge it is. The auditor picks
# the kind; the basis follows from it, because the basis is what the estimate keys on and a
# pair that disagree would bill one thing and display another.
_BASIS_FOR_CLASS: dict[str, str] = {
    RECURRING: UnitBasis.PER_VEHICLE_PER_MONTH.value,
    PER_DRIVER: UnitBasis.PER_DRIVER_PER_MONTH.value,
    ONE_TIME: UnitBasis.ONE_TIME.value,
    USAGE: UnitBasis.PER_TRANSACTION.value,
    CREDIT: UnitBasis.PERCENT_OF_PROCEEDS.value,
}


def _fee_item(record: dict[str, Any]) -> dict[str, Any] | None:
    """Project a pricing term into the fee-item shape the estimate consumes.

    The unit basis is derived from the contract's own frequency wording rather than trusted
    from the model, because that one field decides whether a charge enters recurring dues.

    An override set during the billing audit wins over both. It is kept beside the reading
    rather than written over it: the citation still points at what the contract says, and the
    screen can show that the two differ, which is the whole reason somebody looked.
    """
    from .frequency import classify

    found = classify(record.get("frequency"), record.get("calculation"))
    amount = record.get("amount")
    tiers = record.get("tier_bands") or []
    unit_basis, billing_class = found.unit_basis, found.billing_class

    override = record.get("billing_override") or {}
    corrected_fields = []
    if "amount" in override:
        if override["amount"] != amount:
            corrected_fields.append("amount")
        amount = override["amount"]
    if override.get("billing_class"):
        if override["billing_class"] != billing_class:
            corrected_fields.append("billing_class")
        billing_class = override["billing_class"]
        # Only the auditor's own basis survives; keeping the contract's would leave a charge
        # called recurring that the estimate refuses to count.
        unit_basis = _BASIS_FOR_CLASS.get(billing_class, unit_basis)

    if amount is None and not tiers and record.get("rate_pct") is None:
        return None  # a program named with nothing priced under it bills nothing
    return {
        "description": record.get("item") or record.get("program") or "",
        "fee_type": record.get("fee_type") or "other",
        "amount": amount,
        "currency": record.get("currency") or "USD",
        "rate_pct": record.get("rate_pct"),
        "unit_basis": unit_basis,
        "billing_class": billing_class,
        "minimum": record.get("minimum"),
        "maximum": record.get("maximum"),
        "tier_bands": tiers,
        "conditions": record.get("conditions") or [],
        # What the contract was read as saying, so a correction is visible rather than a
        # number that silently disagrees with the clause beside it.
        "corrected": corrected_fields,
        "as_read": {"amount": record.get("amount"), "billing_class": found.billing_class},
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
