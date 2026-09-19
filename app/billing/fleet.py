"""Effective fleet size: derived from the vehicle inventory, overridable by a provider.

`Engagement.fleet_size` stays a stored scalar holding the *effective* value, so the finance
dashboard can read it across every engagement without a per-engagement count query, and so a
change in dues is always an explicit write rather than a side effect of moving a vehicle.

The lock matters: `billing/schedule.py` recomputes every unpaid installment from the
engagement's current `monthly_recurring`, so re-deriving the fleet after billing goes live
would retroactively change amounts already invoiced.
"""

from __future__ import annotations

# Mirrors the 409 guard in api/engagements.py.
# Only once billing is live. The reason to lock is that unpaid installments recompute from the
# engagement's current dues, so re-deriving the fleet would retroactively change amounts
# already invoiced — and nothing is invoiced before go-live. Locking from billing setup
# onwards meant an engagement whose vehicles arrived late reached the audit at zero vehicles,
# billing nothing a month, with the one number that mattered no longer editable.
LOCKED_STATUSES = frozenset({"ACTIVE"})


def effective_fleet_size(assigned_vehicle_count: int, override: int | None) -> int:
    """The override wins when set; otherwise the count of assigned vehicles."""
    if override is not None:
        return max(1, override)
    return max(0, assigned_vehicle_count)


def fleet_source(override: int | None) -> str:
    return "override" if override is not None else "derived"


def is_locked(status: str | None) -> bool:
    return (status or "") in LOCKED_STATUSES
