"""Payment actions on the billing schedule: record a payment, or remind the client of a due."""

from __future__ import annotations

import datetime
import logging

from fastapi import APIRouter, Depends, HTTPException

from ..auth.deps import get_principal, get_repo, membership_dep, require_provider
from ..auth.principal import Principal
from ..billing.schedule import enrich
from ..config import get_settings
from ..lifecycle.submission_state import Role
from ..store.models import Membership, Payment
from ..store.repository import Repository, utcnow

log = logging.getLogger(__name__)
router = APIRouter(tags=["payments"])


def _row(repo: Repository, engagement_id: str, seq: int) -> tuple[Payment, dict]:
    payment = repo.get_payment(engagement_id, seq)
    if payment is None:
        raise HTTPException(404, "payment not found")
    engagement = repo.get_engagement(engagement_id)
    rows = enrich(
        repo.list_payments(engagement_id),
        engagement.monthly_recurring if engagement else None,
        engagement.billing_frequency if engagement else "monthly",
        datetime.date.today(),
    )
    enriched = next(r for r in rows if r["seq"] == seq)
    return payment, enriched


def _members(repo: Repository, engagement_id: str, roles: tuple[Role, ...]) -> list[Membership]:
    return [m for m in repo.list_members(engagement_id) if m.role in roles]


def _billing_url(engagement_id: str) -> str:
    return f"{get_settings().ui_url}/engagements/{engagement_id}/billing"


def _due(iso: str) -> str:
    return datetime.date.fromisoformat(iso).strftime("%d %b %Y")


def _send(recipients: list[Membership], template: str, **ctx) -> None:
    if not recipients:
        return
    from ..notify.emailer import Emailer

    emailer = Emailer()
    for m in recipients:
        try:
            emailer.send(to=m.email, template=template, recipient_name=m.name or m.email, **ctx)
        except Exception as e:  # noqa: BLE001 - one bad recipient shouldn't drop the rest
            log.warning("payment email %s -> %s failed: %s", template, m.email, e)


@router.post("/engagements/{engagement_id}/payments/{seq}:pay")
def pay(
    engagement_id: str,
    seq: int,
    member: Membership = Depends(membership_dep),
    principal: Principal = Depends(get_principal),
    repo: Repository = Depends(get_repo),
):
    payment, enriched = _row(repo, engagement_id, seq)
    if payment.paid:
        raise HTTPException(409, "this payment is already paid")
    if not enriched["payable"]:
        raise HTTPException(409, "this payment is not payable yet")
    payer = member.name or principal.name or principal.email
    payment.paid = True
    payment.paid_at = utcnow()
    payment.paid_by = payer
    payment.amount_paid = enriched["amount"]
    repo.put_payment(payment)

    engagement = repo.get_engagement(engagement_id)
    amount_str = f"${enriched['amount']:,.2f}"
    ctx = {
        "engagement_name": engagement.name if engagement else engagement_id,
        "amount": amount_str, "period": payment.label, "due_date": _due(payment.due_date),
        "payer_name": payer, "billing_url": _billing_url(engagement_id),
    }
    _send(_members(repo, engagement_id, (Role.PROVIDER, Role.FINANCE)), "PAYMENT_RECEIVED", **ctx)
    _send(_members(repo, engagement_id, (Role.CLIENT,)), "PAYMENT_CONFIRMATION", **ctx)
    return {"ok": True, "seq": seq}


@router.post("/engagements/{engagement_id}/payments/{seq}:remind")
def remind(
    engagement_id: str,
    seq: int,
    member: Membership = Depends(require_provider),
    principal: Principal = Depends(get_principal),
    repo: Repository = Depends(get_repo),
):
    payment, enriched = _row(repo, engagement_id, seq)
    if payment.paid:
        raise HTTPException(409, "this payment is already paid")
    if enriched["status"] not in ("due", "overdue"):
        raise HTTPException(409, "this payment is not due yet")
    engagement = repo.get_engagement(engagement_id)
    clients = _members(repo, engagement_id, (Role.CLIENT,))
    if not clients:
        raise HTTPException(400, "no client on this engagement to remind")
    status_phrase = "is overdue" if enriched["status"] == "overdue" else "is due"
    _send(
        clients, "PAYMENT_REMINDER",
        engagement_name=engagement.name if engagement else engagement_id,
        amount=f"${enriched['amount']:,.2f}", period=payment.label,
        due_date=_due(payment.due_date), status_phrase=status_phrase,
        sender_name=member.name or principal.name or "Your provider team",
        billing_url=_billing_url(engagement_id),
    )
    return {"ok": True, "seq": seq, "reminded": [m.email for m in clients]}
