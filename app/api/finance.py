"""Finance dashboard: org-wide portfolio, recurring revenue, and pipeline aggregates.

The lifecycle status is read from the submissions (the source of truth) joined to engagements;
recurring dues come from the denormalized `monthly_recurring` (set at billing setup / fleet
change), with a best-effort recompute for any active engagement that predates that field.
"""

from __future__ import annotations

import datetime
import json
from collections import defaultdict

from fastapi import APIRouter, Depends

from ..auth.deps import get_repo, get_s3, require_provider_principal
from ..auth.principal import Principal
from ..billing.estimate import compute_monthly_recurring
from ..billing.schedule import enrich, summarize
from ..objects import billing_config_key
from ..store.repository import Repository
from ..store.s3 import S3Store

router = APIRouter(tags=["finance"])

# Pipeline stages, in order, mapping submission statuses into finance-meaningful buckets.
_STAGES: list[tuple[str, str, set[str]]] = [
    ("processing", "Processing", {"DRAFT", "EXTRACTING", "REVALIDATING"}),
    ("underwriting", "Underwriting", {"IN_UNDERWRITING", "VALIDATION_FAILED"}),
    ("client", "With client", {"PENDING_CLIENT_APPROVAL", "CHANGES_REQUESTED_CLIENT"}),
    (
        "finance",
        "Awaiting finance",
        {"CLIENT_APPROVED", "PENDING_FINANCE_APPROVAL", "CHANGES_REQUESTED_FINANCE"},
    ),
    ("billing", "Billing setup", {"FINANCE_APPROVED", "BILLING_SETUP"}),
    ("active", "Active", {"ACTIVE"}),
]
_AWAITING_FINANCE = {"CLIENT_APPROVED", "PENDING_FINANCE_APPROVAL"}


def _dues(s3: S3Store, engagement, submission) -> float | None:
    """Persisted recurring dues, recomputed from the billing config if missing (legacy rows)."""
    if engagement.monthly_recurring is not None:
        return engagement.monthly_recurring
    if submission is None:
        return None
    try:
        key = billing_config_key(engagement.engagement_id, submission.submission_id)
        raw = s3.get_bytes(key)
        return compute_monthly_recurring(json.loads(raw), engagement.fleet_size or 100)
    except Exception:  # noqa: BLE001 - no config yet
        return None


@router.get("/finance/dashboard")
def finance_dashboard(
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
    s3: S3Store = Depends(get_s3),
):
    engagements = repo.list_all_engagements()
    subs = {s.engagement_id: s for s in repo.list_all_submissions()}
    payments_by_eid: dict[str, list] = defaultdict(list)
    for p in repo.list_all_payments():
        payments_by_eid[p.engagement_id].append(p)
    today = datetime.date.today()

    rows = []
    for e in engagements:
        sub = subs.get(e.engagement_id)
        status = sub.status.value if sub else e.status
        dues = _dues(s3, e, sub) if status == "ACTIVE" else e.monthly_recurring
        miss = {"overdue_count": 0, "overdue_amount": 0.0}
        if payments_by_eid.get(e.engagement_id):
            enriched = enrich(payments_by_eid[e.engagement_id], e.monthly_recurring,
                              e.billing_frequency, today)
            miss = summarize(enriched)
        rows.append(
            {
                "engagement_id": e.engagement_id,
                "name": e.name,
                "client_name": e.client_name,
                "status": status,
                "fleet_size": e.fleet_size,
                "monthly_recurring": dues,
                "annualized": round((dues or 0) * 12, 2) if dues else None,
                "overdue_count": miss["overdue_count"],
                "overdue_amount": miss["overdue_amount"],
            }
        )

    active = [r for r in rows if r["status"] == "ACTIVE"]
    monthly = round(sum(r["monthly_recurring"] or 0 for r in active), 2)
    total_fleet = sum(r["fleet_size"] or 0 for r in active)
    missed_amount = round(sum(r["overdue_amount"] for r in rows), 2)
    missed_count = sum(r["overdue_count"] for r in rows)

    funnel = [
        {"key": key, "label": label, "count": sum(1 for r in rows if r["status"] in statuses)}
        for key, label, statuses in _STAGES
    ]
    # Overdue first, then active + highest-revenue, then the rest of the pipeline.
    rows.sort(key=lambda r: (-r["overdue_amount"], r["status"] != "ACTIVE",
                             -(r["monthly_recurring"] or 0)))

    return {
        "totals": {
            "engagements": len(rows),
            "active": len(active),
            "awaiting_finance": sum(1 for r in rows if r["status"] in _AWAITING_FINANCE),
            "in_pipeline": len(rows) - len(active),
            "monthly_recurring": monthly,
            "annualized": round(monthly * 12, 2),
            "avg_monthly": round(monthly / len(active), 2) if active else 0,
            "total_fleet": total_fleet,
            "missed_count": missed_count,
            "missed_amount": missed_amount,
            "missed_engagements": sum(1 for r in rows if r["overdue_count"] > 0),
        },
        "funnel": funnel,
        "engagements": rows,
    }
