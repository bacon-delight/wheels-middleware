"""Effective fleet size: derived from assigned vehicles, overridden by an explicit number."""

from __future__ import annotations

from app.billing.fleet import effective_fleet_size, fleet_source, is_locked


def test_derived_from_assigned_count_when_no_override():
    assert effective_fleet_size(40, None) == 40
    assert fleet_source(None) == "derived"


def test_override_wins_over_the_assigned_count():
    assert effective_fleet_size(40, 100) == 100
    assert fleet_source(100) == "override"


def test_derived_zero_when_nothing_is_assigned():
    """A brand-new engagement bills nothing until vehicles land or a number is entered."""
    assert effective_fleet_size(0, None) == 0


def test_override_floors_at_one():
    assert effective_fleet_size(0, 0) == 1


def test_the_fleet_locks_at_go_live_and_not_before():
    """Unpaid installments recompute from the current dues, so re-deriving the fleet after
    go-live would rewrite what has already been invoiced. Before it, nothing has been."""
    assert is_locked("ACTIVE")
    assert not is_locked("BILLING_SETUP")
    assert not is_locked("PENDING_BILLING_AUDIT")
    assert not is_locked("IN_UNDERWRITING")
    assert not is_locked(None)
