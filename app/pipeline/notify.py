"""Notify worker (EventBridge): fan a lifecycle transition out to the right party via SES."""

from __future__ import annotations

import logging

from ..config import get_settings
from ..lifecycle.submission_state import Role
from ..store.repository import Repository

log = logging.getLogger(__name__)

# transition action -> (email template, recipient roles)
_ROUTES: dict[str, tuple[str, list[Role]]] = {
    "submit_to_client": ("TERMS_SUBMITTED_TO_CLIENT", [Role.CLIENT]),
    "client_request_changes": ("CLIENT_CHANGES_REQUESTED", [Role.PROVIDER]),
    "client_approve": ("CLIENT_APPROVED", [Role.PROVIDER, Role.FINANCE]),
    "capture_fields": ("FINANCE_APPROVAL_NEEDED", [Role.PROVIDER, Role.FINANCE]),
    "finance_request_changes": ("FINANCE_CHANGES_REQUESTED", [Role.PROVIDER]),
    "sanity_fail": ("VALIDATION_FAILED", [Role.PROVIDER]),
    "billing_done": ("BILLING_ACTIVE", [Role.PROVIDER, Role.CLIENT, Role.FINANCE]),
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
        "inviter_name": "Wheels",
        "round": detail.get("round", 1),
        "comments": detail.get("comment") or "(no comment provided)",
        "reasons": detail.get("reasons") or "(see the document)",
        "engagement_url": base,
        "review_url": f"{base}/review",
        "finance_url": f"{base}/finance",
    }


def _notify(action: str | None, detail: dict) -> None:
    route = _ROUTES.get(action or "")
    if route is None:
        return
    template, roles = route
    settings = get_settings()
    repo = Repository()
    engagement = repo.get_engagement(detail.get("engagement_id", ""))
    if engagement is None:
        return
    recipients = [m for m in repo.list_members(engagement.engagement_id) if m.role in roles]
    if not recipients:
        return

    from ..notify.emailer import Emailer

    emailer = Emailer(settings)
    ctx = _context(settings, engagement, detail)
    for m in recipients:
        try:
            emailer.send(to=m.email, template=template, recipient_name=m.name or m.email, **ctx)
        except Exception as e:  # noqa: BLE001 - one bad recipient shouldn't drop the rest
            log.warning("notify %s -> %s failed: %s", template, m.email, e)
