"""Amendments: a further review cycle over an engagement that is already live.

An engagement's agreements do not stop changing when billing starts. A master agreement is
renewed, with or without amended terms; a customer on a service-only engagement adds a lease;
a document is reissued as a corrected version. Each of those is a new set of terms, and terms
only take effect once the customer and finance have approved them — the same path the original
agreements took.

So an amendment is not an edit to the completed cycle. It is a new cycle of the same state
machine, running alongside the live one. The live cycle keeps the engagement ACTIVE and keeps
billing on the agreed terms until the amendment reaches ACTIVE in its turn, at which point it
becomes the cycle in force.

This module is the policy only — which cycles may be opened and what they are called — so the
rules sit next to the state machine rather than inside a request handler.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from .submission_state import SubmissionStatus

# Any object carrying `cycle`, `created_at` and `status`; typed structurally so this module
# stays free of the storage models and can be imported from anywhere.
Cycle = TypeVar("Cycle")

# Only a completed cycle can be amended. Any other status means a cycle is already open, and
# the change belongs in that one: before go-live the agreements are still being settled, so a
# new document is simply uploaded to the cycle in progress.
AMENDABLE_STATUSES = frozenset({SubmissionStatus.ACTIVE.value})


def can_open_amendment(status: SubmissionStatus | str | None) -> bool:
    if status is None:
        return False
    value = status.value if isinstance(status, SubmissionStatus) else str(status)
    return value in AMENDABLE_STATUSES


def cycle_label(cycle: int) -> str:
    """What to call a cycle in the interface. Cycle 1 is the agreements the deal started on."""
    return "Original agreement" if cycle <= 1 else f"Amendment {cycle - 1}"


def ordered_cycles(cycles: Iterable[Cycle]) -> list[Cycle]:
    """One engagement's cycles, oldest first.

    Submissions are keyed by a random id, so nothing about how they come back from storage
    reflects the order they ran in. Anything choosing "the latest cycle" sorts first.
    """
    return sorted(cycles, key=lambda s: (s.cycle, s.created_at))


def cycle_in_force(cycles: Iterable[Cycle]) -> Cycle | None:
    """The cycle whose terms are in force: the latest to go live, else the latest opened.

    While an amendment is under review this is the cycle before it, which is what keeps an
    engagement billing on the terms the customer actually signed. Rates, the payment schedule,
    the fleet lock and the finance dashboard all read from here.
    """
    ordered = ordered_cycles(cycles)
    live = [s for s in ordered if s.status == SubmissionStatus.ACTIVE]
    return live[-1] if live else (ordered[-1] if ordered else None)


def open_cycle(cycles: Iterable[Cycle]) -> Cycle | None:
    """The cycle actions apply to: the one still running, else the latest.

    Only one cycle is open at a time, so this is unambiguous.
    """
    ordered = ordered_cycles(cycles)
    unfinished = [s for s in ordered if s.status != SubmissionStatus.ACTIVE]
    return unfinished[-1] if unfinished else (ordered[-1] if ordered else None)
