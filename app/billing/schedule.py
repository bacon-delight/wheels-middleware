"""Payment schedule: turn a billing config + fleet into a dated list of payments.

A schedule has an **initial payment** (due when billing goes active) followed by recurring
payments at the applicable cadence (monthly / quarterly / annual). Due dates are fixed when the
schedule is generated; amounts for unpaid rows are computed from the engagement's current
recurring dues so a fleet change reflows into the future.

Status (relative to `today`, using each row's period window `[due_date, next_due_date)`):
  - paid       — recorded as paid
  - upcoming   — its due date hasn't arrived (disabled; the *next* one offers "pay early")
  - due        — we're inside its period; payable, on time
  - overdue    — the next period already started and it's still unpaid (a missed due)
"""

from __future__ import annotations

import calendar
import datetime
from typing import Any

# frequency -> (months per period == multiplier on monthly dues, number of rows generated)
_FREQ: dict[str, tuple[int, int]] = {
    "monthly": (1, 12),
    "quarterly": (3, 4),
    "annual": (12, 3),
}


def normalize_frequency(config: dict[str, Any] | None) -> str:
    raw = str(((config or {}).get("lease_terms") or {}).get("billing_frequency") or "").lower()
    if "quarter" in raw:
        return "quarterly"
    if "year" in raw or "annual" in raw:
        return "annual"
    return "monthly"


def period_multiplier(frequency: str) -> int:
    return _FREQ.get(frequency, _FREQ["monthly"])[0]


def _add_months(d: datetime.date, months: int) -> datetime.date:
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


def _period_label(frequency: str, start: datetime.date) -> str:
    if frequency == "quarterly":
        return f"Q{(start.month - 1) // 3 + 1} {start.year}"
    if frequency == "annual":
        return str(start.year)
    return start.strftime("%B %Y")


def generate_rows(billing_start: datetime.date, frequency: str) -> list[dict[str, Any]]:
    """The fixed skeleton (seq/kind/label/dates) for a fresh schedule."""
    months, count = _FREQ.get(frequency, _FREQ["monthly"])
    rows = []
    for i in range(count):
        start = _add_months(billing_start, i * months)
        rows.append(
            {
                "seq": i,
                "kind": "initial" if i == 0 else "recurring",
                "label": "Initial payment" if i == 0 else _period_label(frequency, start),
                "period_start": start.isoformat(),
                "due_date": start.isoformat(),
            }
        )
    return rows


def _d(iso: str) -> datetime.date:
    return datetime.date.fromisoformat(iso)


def enrich(
    payments: list, monthly_recurring: float | None, frequency: str, today: datetime.date
) -> list[dict[str, Any]]:
    """Attach amount + status + payability to each persisted payment, in due-date order."""
    rows = sorted(payments, key=lambda p: p.seq)
    months = period_multiplier(frequency)
    period_amount = round((monthly_recurring or 0) * months, 2)

    unpaid_upcoming = [p.seq for p in rows if not p.paid and _d(p.due_date) > today]
    next_early = min(unpaid_upcoming, default=None)

    out = []
    for i, p in enumerate(rows):
        due = _d(p.due_date)
        nxt = _d(rows[i + 1].due_date) if i + 1 < len(rows) else _add_months(due, months)
        if p.paid:
            status = "paid"
        elif today < due:
            status = "upcoming"
        elif today < nxt:
            status = "due"
        else:
            status = "overdue"
        pay_early = status == "upcoming" and p.seq == next_early
        out.append(
            {
                "seq": p.seq,
                "kind": p.kind,
                "label": p.label,
                "due_date": p.due_date,
                "amount": p.amount_paid if p.paid else period_amount,
                "status": status,
                "paid_at": p.paid_at,
                "paid_by": p.paid_by,
                "pay_early": pay_early,
                "payable": status in ("due", "overdue") or pay_early,
            }
        )
    return out


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    overdue = [r for r in rows if r["status"] == "overdue"]
    paid = [r for r in rows if r["status"] == "paid"]
    return {
        "overdue_count": len(overdue),
        "overdue_amount": round(sum(r["amount"] for r in overdue), 2),
        "paid_count": len(paid),
        "paid_amount": round(sum(r["amount"] for r in paid), 2),
    }
