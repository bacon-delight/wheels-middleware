"""What a customer is shown of an engagement's internal machinery: as little as possible.

A customer is a party to the contract, not a user of the workflow. Extraction, underwriting and
finance validation are how Wheels arrives at the terms; the customer's concern is that terms
arrive, that they get to review them, and that billing starts. Showing them the internal steps
invites questions about a process they have no part in, and leaks how the sausage is made.
"""

from __future__ import annotations

# Audit actions a customer may see in the activity feed. Everything else — extraction, sanity
# checks, per-term approvals, finance's internal back-and-forth, vehicle and document
# housekeeping — belongs to the provider side.
CLIENT_VISIBLE_ACTIONS: frozenset[str] = frozenset({
    "engagement_created",
    # An amendment is a change to the deal the customer signed, so the fact one was opened is
    # theirs to see — the documents and extraction behind it are not.
    "amendment_opened",
    "submit_to_client",
    "client_approve",
    "client_request_changes",
    "resubmit_to_client",
    "setup_billing",
    "billing_done",
    "user_invited",
})


def visible_actions(is_client: bool) -> frozenset[str] | None:
    """The allowlist to filter an audit trail by, or None for no filtering."""
    return CLIENT_VISIBLE_ACTIONS if is_client else None
