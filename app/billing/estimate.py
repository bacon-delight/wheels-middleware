"""Estimate the recurring monthly dues from a generated billing config.

Mirrors the UI's client-side estimate so the finance dashboard, the client billing view, and
the provider's live estimate all agree: recurring per-vehicle-per-month flat fees plus the
applicable bundled tier band, multiplied by the fleet size. Usage and pass-through charges
(per-transaction, per-claim, percentage) are excluded — they bill separately.
"""

from __future__ import annotations

import re
from typing import Any

_PER_UNIT_MONTH = re.compile(r"per_(vehicle|unit).*month")


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _monthly_per_unit(fi: dict[str, Any]) -> bool:
    return _num(fi.get("amount")) is not None and bool(
        _PER_UNIT_MONTH.search(fi.get("unit_basis") or "")
    )


def compute_monthly_recurring(config: dict[str, Any] | None, fleet_size: int) -> float:
    """Recurring monthly dues for `fleet_size` vehicles under the given billing config."""
    if not config or fleet_size <= 0:
        return 0.0
    per_unit = 0.0
    for sl in config.get("service_lines", []) or []:
        for fi in sl.get("fee_items", []) or []:
            amount = _num(fi.get("amount"))
            if amount is not None and _monthly_per_unit(fi):
                per_unit += amount
            for band in fi.get("tier_bands") or []:
                lo = band.get("min_units") or 0
                hi = band.get("max_units")
                if fleet_size >= lo and (hi is None or fleet_size <= hi):
                    band_amt = _num(band.get("amount"))
                    if band_amt is not None:
                        per_unit += band_amt
                    break  # first matching band only
    return round(per_unit * fleet_size, 2)
