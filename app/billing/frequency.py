"""Turn a contract's own words about frequency into something billing can consume.

Contracts write the same idea a dozen ways — "pvpm", "per vehicle per month", "per unit per
month", "Per Vehicle Per Month" — and a per-vehicle estimate can only use some of them. This
maps the wording onto the unit basis the estimate already matches on, and says what *kind* of
charge it is so the ones that cannot enter a per-vehicle figure are shown rather than dropped.

Three behaviours worth stating out loud, because each looks like a bug otherwise:

* **Per-card monthly fees do not count** toward recurring dues. That is deliberate and
  long-standing: the fleet size is a count of vehicles, not of cards.
* **Per-driver fees do not count either**, for the same reason — there is no driver count. They
  are classified so the interface can show them separately instead of pretending they are free.
* **Rebates and incentives are credits**, not charges, and must never be added to what a
  customer owes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..extraction.schema import UnitBasis

# What a charge is, for display and for whether it can enter a per-vehicle estimate.
RECURRING = "recurring"  # per vehicle per month: the estimate is built from these
PER_DRIVER = "recurring_per_driver"  # recurring, but not per vehicle
USAGE = "usage"  # per transaction, per card, per claim: billed as incurred
ONE_TIME = "one_time"
CREDIT = "credit"  # rebates and incentives: owed to the customer, never charged


@dataclass(frozen=True)
class Frequency:
    unit_basis: str
    billing_class: str

    @property
    def in_recurring_estimate(self) -> bool:
        return self.billing_class == RECURRING


# Ordered: the first pattern that matches wins, so the specific comes before the general.
_RULES: tuple[tuple[str, str, str], ...] = (
    # Credits first — "rebate ... per litre" is a credit, not a usage charge.
    (r"rebate|incentive|revenue shar|credit back", UnitBasis.PERCENT_OF_PROCEEDS.value, CREDIT),
    (r"per\s*(vehicle|unit)\s*per\s*month|pvpm|per\s*vehicle\s*/\s*month",
     UnitBasis.PER_VEHICLE_PER_MONTH.value, RECURRING),
    (r"per\s*driver\s*per\s*month", UnitBasis.PER_DRIVER_PER_MONTH.value, PER_DRIVER),
    (r"per\s*card\s*per\s*month|per\s*card", UnitBasis.PER_CARD_PER_MONTH.value, USAGE),
    (r"per\s*claim|per\s*loss", UnitBasis.PER_CLAIM.value, USAGE),
    (r"per\s*incident|per\s*occurrence|per\s*notice|per\s*violation",
     UnitBasis.PER_INCIDENT.value, USAGE),
    (r"per\s*transaction|per\s*request|per\s*issuance|per\s*module|per\s*replacement|"
     r"per\s*order|per\s*report|per\s*enrol|for\s*enrol",
     UnitBasis.PER_TRANSACTION.value, USAGE),
    (r"percent\s*of\s*proceeds|of\s*(the\s*)?(sale|auction)\s*proceeds",
     UnitBasis.PERCENT_OF_PROCEEDS.value, USAGE),
    (r"\bbps\b|basis\s*points|%\s*of|percent\s*\(|ten[- ]percent|percentage\s*of",
     UnitBasis.PERCENT_OF_COST.value, USAGE),
    # A pass-through plus a markup: "Appraiser Fee + $50", "cost plus 8%". Billed as incurred.
    (r"\+\s*\$|cost\s*plus|at\s*cost\b|pass[- ]through", UnitBasis.PERCENT_OF_COST.value, USAGE),
    (r"one[- ]time|upfront|at\s*inception|on\s*delivery", UnitBasis.ONE_TIME.value, ONE_TIME),
    (r"per\s*month|monthly", UnitBasis.PER_UNIT_PER_MONTH.value, RECURRING),
    (r"per\s*unit|per\s*vehicle", UnitBasis.PER_UNIT.value, USAGE),
)

_COMPILED = tuple((re.compile(p, re.IGNORECASE), basis, cls) for p, basis, cls in _RULES)

UNKNOWN = Frequency(UnitBasis.OTHER.value, USAGE)


def classify(frequency: str | None, calculation: str | None = None) -> Frequency:
    """Read the frequency, falling back to the calculation text when the column is empty.

    A contract often leaves the frequency column blank and says it in the formula instead:
    "Ten-percent (10%) of ISP Charges per transaction".
    """
    text = " ".join(part for part in (frequency, calculation) if part).strip()
    if not text:
        return UNKNOWN
    for pattern, basis, billing_class in _COMPILED:
        if pattern.search(text):
            return Frequency(basis, billing_class)
    return UNKNOWN


def unit_basis_for(frequency: str | None, calculation: str | None = None) -> str:
    return classify(frequency, calculation).unit_basis
