"""Frequency wording decides what a customer is billed, so it is worth pinning down."""

from __future__ import annotations

import json
import pathlib

import pytest

from app.billing.frequency import CREDIT, PER_DRIVER, RECURRING, USAGE, classify

GROUND_TRUTH = pathlib.Path(__file__).parent / "fixtures" / "walmart_ground_truth.json"


@pytest.mark.parametrize(
    "text",
    ["pvpm", "per vehicle per month", "Per Vehicle Per Month", "per unit per month", "$1.00 pvpm"],
)
def test_the_many_ways_of_saying_per_vehicle_per_month(text):
    assert classify(text).billing_class == RECURRING


def test_a_per_card_monthly_fee_is_not_recurring_dues():
    """Deliberate and long-standing: the fleet size counts vehicles, not cards.

    The regex behind this looks like an oversight, so this test states the intent.
    """
    found = classify("per card")
    assert found.billing_class == USAGE
    assert found.in_recurring_estimate is False


def test_a_per_driver_fee_is_recognised_but_kept_out_of_a_per_vehicle_estimate():
    found = classify("per driver per month")
    assert found.billing_class == PER_DRIVER
    assert found.in_recurring_estimate is False


def test_a_rebate_is_a_credit_not_a_charge():
    assert classify("per litre; paid quarterly", "rebate on qualifying fuel spend").billing_class == CREDIT
    assert classify(None, "revenue sharing on fuel purchases").billing_class == CREDIT


def test_the_formula_is_read_when_the_frequency_column_is_blank():
    found = classify(None, "Ten-percent (10%) of ISP Charges per transaction")
    assert found.billing_class == USAGE


def test_every_frequency_in_the_workbook_is_understood():
    """Anything unrecognised is excluded from dues, so an unread frequency is lost revenue."""
    records = json.loads(GROUND_TRUTH.read_text())["records"]
    unknown = []
    for r in records:
        if r["info_type"] != "pricing_item":
            continue
        text = r.get("frequency") or r.get("calculation")
        if not text:
            continue
        if classify(r.get("frequency"), r.get("calculation")).unit_basis == "other":
            unknown.append(text)
    assert unknown == [], f"frequencies with no rule: {sorted(set(unknown))}"
