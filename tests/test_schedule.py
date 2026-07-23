"""Pure unit tests for the payment schedule logic (status windows, amounts, cadence)."""

from __future__ import annotations

import datetime

from app.billing.schedule import enrich, generate_rows, summarize
from app.store.models import Payment


def _payments(start: datetime.date, frequency: str) -> list[Payment]:
    return [
        Payment(engagement_id="e", submission_id="s", **row)
        for row in generate_rows(start, frequency)
    ]


def test_monthly_schedule_status_windows():
    payments = _payments(datetime.date(2026, 1, 15), "monthly")
    assert len(payments) == 12
    today = datetime.date(2026, 3, 20)  # Jan & Feb fully past, we're inside March's window
    rows = {r["seq"]: r for r in enrich(payments, 100.0, "monthly", today)}
    assert rows[0]["status"] == "overdue"  # Jan (next due Feb 15 < today)
    assert rows[1]["status"] == "overdue"  # Feb (next due Mar 15 < today)
    assert rows[2]["status"] == "due" and rows[2]["payable"]  # March, current period
    assert rows[3]["status"] == "upcoming" and rows[3]["pay_early"] and rows[3]["payable"]
    assert rows[4]["status"] == "upcoming" and not rows[4]["pay_early"] and not rows[4]["payable"]
    assert rows[0]["amount"] == 100.0  # monthly_recurring x 1


def test_paid_freezes_amount_and_summary():
    payments = _payments(datetime.date(2026, 1, 15), "monthly")
    today = datetime.date(2026, 3, 20)
    payments[0].paid = True
    payments[0].amount_paid = 90.0
    rows = {r["seq"]: r for r in enrich(payments, 100.0, "monthly", today)}
    assert rows[0]["status"] == "paid" and rows[0]["amount"] == 90.0
    summary = summarize(enrich(payments, 100.0, "monthly", today))
    assert summary["overdue_count"] == 1  # only Feb now that Jan is paid
    assert summary["paid_count"] == 1 and summary["paid_amount"] == 90.0


def test_quarterly_multiplier_and_labels():
    payments = _payments(datetime.date(2026, 1, 1), "quarterly")
    assert len(payments) == 4
    rows = enrich(payments, 100.0, "quarterly", datetime.date(2026, 1, 15))
    assert rows[0]["amount"] == 300.0  # monthly x 3
    assert rows[1]["label"] == "Q2 2026"
