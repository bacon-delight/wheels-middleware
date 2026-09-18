"""Finance dashboard: portfolio revenue, collections, concentration and pipeline.

Built around the questions a finance team actually asks: how much recurring revenue is on the
book, who it comes from, how much of it has been collected, what is late and how late, and what
is coming next. Revenue rolls up to the customer, not just the engagement, because one customer
signs repeat deals and the exposure that matters is the customer's total.

Lifecycle status is read from the submissions (the source of truth) joined to engagements;
recurring dues come from the denormalized `monthly_recurring` set at billing setup, with a
best-effort recompute for any active engagement that predates that field.
"""

from __future__ import annotations

import datetime
import json
from collections import defaultdict

from fastapi import APIRouter, Depends, Query

from ..auth.deps import get_repo, get_s3, require_provider_principal
from ..auth.principal import Principal
from ..billing.estimate import compute_monthly_recurring
from ..billing.schedule import enrich, summarize
from ..lifecycle.amendment import cycle_in_force
from ..objects import billing_config_key
from ..store.repository import Repository
from ..store.s3 import S3Store

router = APIRouter(tags=["finance"])

# Pipeline stages, in order, mapping submission statuses into finance-meaningful buckets.
_STAGES: list[tuple[str, str, set[str]]] = [
    ("processing", "Processing", {"DRAFT", "EXTRACTING", "REVALIDATING"}),
    ("underwriting", "Underwriting", {"IN_UNDERWRITING", "VALIDATION_FAILED"}),
    ("client", "With customer", {"PENDING_CLIENT_APPROVAL", "CHANGES_REQUESTED_CLIENT"}),
    (
        "finance",
        "Awaiting finance",
        {"CLIENT_APPROVED", "PENDING_FINANCE_APPROVAL", "CHANGES_REQUESTED_FINANCE"},
    ),
    ("billing", "Billing setup", {"FINANCE_APPROVED", "BILLING_SETUP"}),
    ("active", "Active", {"ACTIVE"}),
]
_AWAITING_FINANCE = {"CLIENT_APPROVED", "PENDING_FINANCE_APPROVAL"}

# Contract-expiry windows, in days from today. Anything already past is "expired".
_EXPIRY: list[tuple[str, str, int | None, int | None]] = [
    ("expired", "Expired", None, -1),
    ("d30", "Within 30 days", 0, 30),
    ("d60", "31-60 days", 31, 60),
    ("d90", "61-90 days", 61, 90),
    ("d180", "91-180 days", 91, 180),
    ("later", "Beyond 180 days", 181, None),
]

# Receivables aging buckets, in days past due.
_AGING: list[tuple[str, str, int, int | None]] = [
    ("current", "Not yet due", -10_000, 0),
    ("d1_30", "1-30 days", 1, 30),
    ("d31_60", "31-60 days", 31, 60),
    ("d61_90", "61-90 days", 61, 90),
    ("d90_plus", "90+ days", 91, None),
]


def _dues(s3: S3Store, engagement, submission) -> float | None:
    """Persisted recurring dues, recomputed from the billing config if missing (legacy rows)."""
    if engagement.monthly_recurring is not None:
        return engagement.monthly_recurring
    if submission is None:
        return None
    try:
        key = billing_config_key(engagement.engagement_id, submission.submission_id)
        raw = s3.get_bytes(key)
        return compute_monthly_recurring(json.loads(raw), engagement.fleet_size or 0)
    except Exception:  # noqa: BLE001 - no config yet
        return None


def _age_bucket(due: datetime.date, today: datetime.date) -> str:
    days = (today - due).days
    for key, _label, lo, hi in _AGING:
        if days >= lo and (hi is None or days <= hi):
            return key
    return "current"


def _expiry_bucket(days: int) -> str:
    for key, _label, lo, hi in _EXPIRY:
        if (lo is None or days >= lo) and (hi is None or days <= hi):
            return key
    return "later"


def _pct(part: float, whole: float) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


@router.get("/finance/dashboard")
def finance_dashboard(
    top: int = Query(default=10, ge=1, le=50, description="How many rows in each ranking"),
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
    s3: S3Store = Depends(get_s3),
):
    engagements = repo.list_all_engagements()
    # An engagement can hold several review cycles. Finance reads the one whose terms are in
    # force and billing, which while an amendment is under review is the cycle before it —
    # keying a plain dict by engagement would have kept whichever the scan returned last.
    by_engagement: dict[str, list] = defaultdict(list)
    for s in repo.list_all_submissions():
        by_engagement[s.engagement_id].append(s)
    subs = {eid: cycle_in_force(cycles) for eid, cycles in by_engagement.items()}
    customers = {c.customer_id: c for c in repo.list_customers()}
    payments_by_eid: dict[str, list] = defaultdict(list)
    for p in repo.list_all_payments():
        payments_by_eid[p.engagement_id].append(p)
    today = datetime.date.today()

    aging = {key: {"key": key, "label": label, "count": 0, "amount": 0.0}
             for key, label, _lo, _hi in _AGING}
    billed_by_month: dict[str, dict[str, float]] = {}

    rows = []
    for e in engagements:
        sub = subs.get(e.engagement_id)
        status = sub.status.value if sub else e.status
        dues = _dues(s3, e, sub) if status == "ACTIVE" else e.monthly_recurring
        stats = {"overdue_count": 0, "overdue_amount": 0.0,
                 "paid_count": 0, "paid_amount": 0.0}
        outstanding = 0.0
        schedule = payments_by_eid.get(e.engagement_id)
        if schedule:
            enriched = enrich(schedule, e.monthly_recurring, e.billing_frequency, today)
            stats = summarize(enriched)
            for r in enriched:
                month = r["due_date"][:7]
                # History only. Future scheduled rows are not "billed" yet, and charting them
                # would show collections falling off a cliff against dues nobody owes.
                if month <= today.isoformat()[:7]:
                    slot = billed_by_month.setdefault(month, {"billed": 0.0, "collected": 0.0})
                    slot["billed"] += r["amount"]
                    if r["status"] == "paid":
                        slot["collected"] += r["amount"]
                if r["status"] in ("due", "overdue"):
                    outstanding += r["amount"]
                    bucket = aging[_age_bucket(datetime.date.fromisoformat(r["due_date"]), today)]
                    bucket["count"] += 1
                    bucket["amount"] += r["amount"]
                else:  # upcoming
                    bucket = aging["current"]
                    bucket["count"] += 1
                    bucket["amount"] += r["amount"]

        customer = customers.get(e.customer_id) if e.customer_id else None
        rows.append(
            {
                "engagement_id": e.engagement_id,
                "name": e.name,
                "customer_id": e.customer_id,
                "customer_name": customer.legal_name if customer else e.client_name,
                "client_name": e.client_name,
                "scope": e.scope,
                "status": status,
                "created_at": e.created_at,
                "fleet_size": e.fleet_size,
                "monthly_recurring": dues,
                "annualized": round((dues or 0) * 12, 2) if dues else None,
                "contract_end": e.contract_end,
                "contract_term_months": e.contract_term_months,
                "auto_renew": e.auto_renew,
                "days_to_expiry": (
                    (datetime.date.fromisoformat(e.contract_end) - today).days
                    if e.contract_end else None
                ),
                "collected_amount": stats["paid_amount"],
                "collected_count": stats["paid_count"],
                "outstanding_amount": round(outstanding, 2),
                "overdue_count": stats["overdue_count"],
                "overdue_amount": stats["overdue_amount"],
            }
        )

    active = [r for r in rows if r["status"] == "ACTIVE"]
    monthly = round(sum(r["monthly_recurring"] or 0 for r in active), 2)
    total_fleet = sum(r["fleet_size"] or 0 for r in active)
    collected = round(sum(r["collected_amount"] for r in rows), 2)
    outstanding_total = round(sum(r["outstanding_amount"] for r in rows), 2)
    missed_amount = round(sum(r["overdue_amount"] for r in rows), 2)
    missed_count = sum(r["overdue_count"] for r in rows)
    # Pipeline value: recurring dues already attached to engagements not yet active.
    pipeline_value = round(sum(r["monthly_recurring"] or 0 for r in rows
                               if r["status"] != "ACTIVE"), 2)

    # --- customer rollup: one customer's total exposure across all their engagements ---
    by_customer: dict[str, dict] = {}
    for r in rows:
        key = r["customer_id"] or f"legacy:{r['client_name']}"
        c = by_customer.setdefault(key, {
            "customer_id": r["customer_id"], "name": r["customer_name"],
            "engagements": 0, "active_engagements": 0, "fleet_size": 0,
            "monthly_recurring": 0.0, "collected_amount": 0.0,
            "outstanding_amount": 0.0, "overdue_amount": 0.0, "overdue_count": 0,
        })
        c["engagements"] += 1
        if r["status"] == "ACTIVE":
            c["active_engagements"] += 1
            c["fleet_size"] += r["fleet_size"] or 0
            c["monthly_recurring"] += r["monthly_recurring"] or 0
        c["collected_amount"] += r["collected_amount"]
        c["outstanding_amount"] += r["outstanding_amount"]
        c["overdue_amount"] += r["overdue_amount"]
        c["overdue_count"] += r["overdue_count"]
    for c in by_customer.values():
        c["monthly_recurring"] = round(c["monthly_recurring"], 2)
        c["annualized"] = round(c["monthly_recurring"] * 12, 2)
        c["collected_amount"] = round(c["collected_amount"], 2)
        c["outstanding_amount"] = round(c["outstanding_amount"], 2)
        c["overdue_amount"] = round(c["overdue_amount"], 2)
        c["revenue_share"] = _pct(c["monthly_recurring"], monthly)

    customer_rows = sorted(by_customer.values(), key=lambda c: -c["monthly_recurring"])
    # Concentration: how much of the book rests on the largest few customers.
    top5_share = _pct(sum(c["monthly_recurring"] for c in customer_rows[:5]), monthly)

    top_customers = customer_rows[:top]
    top_engagements = sorted(
        (r for r in rows if r["status"] == "ACTIVE"),
        key=lambda r: -(r["monthly_recurring"] or 0),
    )[:top]
    top_collected = sorted(customer_rows, key=lambda c: -c["collected_amount"])[:top]
    at_risk = sorted((c for c in customer_rows if c["overdue_amount"] > 0),
                     key=lambda c: -c["overdue_amount"])[:top]

    funnel = [
        {"key": key, "label": label, "count": sum(1 for r in rows if r["status"] in statuses),
         "value": round(sum(r["monthly_recurring"] or 0 for r in rows
                            if r["status"] in statuses), 2)}
        for key, label, statuses in _STAGES
    ]
    trend = [{"month": m, **{k: round(v, 2) for k, v in vals.items()}}
             for m, vals in sorted(billed_by_month.items())]
    # Newest first, for the dashboard's "recent" panel.
    recent = sorted(rows, key=lambda r: r["created_at"] or "", reverse=True)[:top]

    # --- contract expiry ---
    # Only active engagements can expire in a way anyone needs to act on; a deal still in
    # underwriting has no live term to renew.
    dated = [r for r in rows if r["days_to_expiry"] is not None and r["status"] == "ACTIVE"]
    expiry_buckets = {
        key: {"key": key, "label": label, "count": 0, "monthly_recurring": 0.0}
        for key, label, _lo, _hi in _EXPIRY
    }
    for r in dated:
        b = expiry_buckets[_expiry_bucket(r["days_to_expiry"])]
        b["count"] += 1
        b["monthly_recurring"] += r["monthly_recurring"] or 0
    for b in expiry_buckets.values():
        b["monthly_recurring"] = round(b["monthly_recurring"], 2)
    # Soonest first; anything already expired leads, since it is the most urgent.
    expiring = sorted(dated, key=lambda r: r["days_to_expiry"])
    # Revenue whose term ends inside the renewal window, i.e. what is up for renegotiation.
    at_renewal = [r for r in dated if r["days_to_expiry"] <= 90]
    renewal_value = round(sum(r["monthly_recurring"] or 0 for r in at_renewal), 2)

    # Overdue first, then active + highest-revenue, then the rest of the pipeline.
    rows.sort(key=lambda r: (-r["overdue_amount"], r["status"] != "ACTIVE",
                             -(r["monthly_recurring"] or 0)))

    return {
        "totals": {
            "engagements": len(rows),
            "active": len(active),
            "awaiting_finance": sum(1 for r in rows if r["status"] in _AWAITING_FINANCE),
            "in_pipeline": len(rows) - len(active),
            "customers": len(by_customer),
            "active_customers": sum(1 for c in by_customer.values()
                                    if c["active_engagements"] > 0),
            "monthly_recurring": monthly,
            "annualized": round(monthly * 12, 2),
            "avg_per_engagement": round(monthly / len(active), 2) if active else 0,
            "avg_per_customer": round(
                monthly / sum(1 for c in by_customer.values() if c["active_engagements"] > 0), 2
            ) if any(c["active_engagements"] for c in by_customer.values()) else 0,
            "total_fleet": total_fleet,
            "revenue_per_vehicle": round(monthly / total_fleet, 2) if total_fleet else 0,
            "pipeline_value": pipeline_value,
            "collected_amount": collected,
            "outstanding_amount": outstanding_total,
            "collection_rate": _pct(collected, collected + outstanding_total),
            "missed_count": missed_count,
            "missed_amount": missed_amount,
            "missed_engagements": sum(1 for r in rows if r["overdue_count"] > 0),
            "top5_revenue_share": top5_share,
            "expiring_90d": len(at_renewal),
            "expiring_90d_value": renewal_value,
            "expired": expiry_buckets["expired"]["count"],
        },
        "funnel": funnel,
        "aging": [aging[key] for key, _l, _lo, _hi in _AGING],
        "top_customers": top_customers,
        "top_engagements": top_engagements,
        "top_collected": top_collected,
        "recent_engagements": recent,
        "expiry_buckets": [expiry_buckets[key] for key, _l, _lo, _hi in _EXPIRY],
        "expiring_contracts": expiring[:top],
        "at_risk_customers": at_risk,
        "revenue_trend": trend,
        "customers": customer_rows,
        "engagements": rows,
    }
