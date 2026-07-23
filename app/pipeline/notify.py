"""Notify worker (EventBridge): fan a lifecycle transition out to the right party via SES."""

from __future__ import annotations

import logging

from ..config import get_settings
from ..lifecycle.submission_state import Role
from ..store.repository import Repository

log = logging.getLogger(__name__)

# transition action -> list of (email template, recipient roles). Each step notifies both the
# party who must act next AND, where useful, the other side with a relevant confirmation.
_PROVIDER = [Role.PROVIDER, Role.FINANCE]
_ROUTES: dict[str, list[tuple[str, list[Role]]]] = {
    "submit_to_client": [("TERMS_SUBMITTED_TO_CLIENT", [Role.CLIENT])],
    "client_request_changes": [
        ("CLIENT_CHANGES_REQUESTED", _PROVIDER),
        ("CLIENT_CHANGES_ACK", [Role.CLIENT]),
    ],
    "resubmit_to_client": [("TERMS_RESUBMITTED", [Role.CLIENT])],
    "reupload": [("CHANGES_APPLIED", [Role.CLIENT])],
    "client_approve": [("CLIENT_APPROVAL_ACK", [Role.CLIENT])],
    "capture_fields": [("FINANCE_APPROVAL_NEEDED", _PROVIDER)],
    "finance_request_changes": [("FINANCE_CHANGES_REQUESTED", _PROVIDER)],
    "sanity_fail": [("VALIDATION_FAILED", _PROVIDER)],
    "billing_done": [
        ("BILLING_ACTIVE_CLIENT", [Role.CLIENT]),
        ("BILLING_CONFIGURED_PROVIDER", _PROVIDER),
    ],
}


def handler(event, context=None):
    detail = event.get("detail", {}) or {}
    action = event.get("detail-type") or event.get("detailType") or detail.get("action")
    _notify(action, detail)
    return {"ok": True}


def _context(settings, engagement, detail) -> dict:
    base = f"{settings.ui_url}/engagements/{engagement.engagement_id}"
    return {
        "engagement_name": engagement.name,
        "inviter_name": detail.get("actor_name") or "Your provider team",
        "round": detail.get("round", 1),
        "comments": detail.get("comment") or "(no comment provided)",
        "reasons": detail.get("reasons") or "(see the document)",
        "engagement_url": base,
        "review_url": f"{base}/review",
        "finance_url": f"{base}/finance",
        "billing_url": f"{base}/billing",
    }


def _notify(action: str | None, detail: dict) -> None:
    routes = _ROUTES.get(action or "")
    if not routes:
        return
    settings = get_settings()
    repo = Repository()
    engagement = repo.get_engagement(detail.get("engagement_id", ""))
    if engagement is None:
        return
    members = repo.list_members(engagement.engagement_id)

    from ..notify.emailer import Emailer

    emailer = Emailer(settings)
    ctx = _context(settings, engagement, detail)
    for template, roles in routes:
        for m in (mm for mm in members if mm.role in roles):
            try:
                emailer.send(to=m.email, template=template, recipient_name=m.name or m.email, **ctx)
            except Exception as e:  # noqa: BLE001 - one bad recipient shouldn't drop the rest
                log.warning("notify %s -> %s failed: %s", template, m.email, e)
