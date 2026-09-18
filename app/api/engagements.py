"""Engagements, membership, invitations, audit trail."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth.deps import get_principal, get_repo, get_s3, membership_dep, require_provider
from ..auth.principal import Principal
from ..billing.fleet import effective_fleet_size, fleet_source, is_locked
from ..lifecycle.amendment import can_open_amendment, cycle_label
from ..lifecycle.submission_state import Role
from ..lifecycle.visibility import visible_actions
from ..store.models import (
    AuditEvent,
    Engagement,
    Membership,
    Submission,
    required_doc_types,
)
from ..store.repository import Repository, new_id, utcnow
from ..store.s3 import S3Store

router = APIRouter(tags=["engagements"])


class CreateEngagementIn(BaseModel):
    name: str
    client_name: str | None = None  # legacy free-text path; ignored when customer_id is given
    customer_id: str | None = None


class UpdateEngagementIn(BaseModel):
    name: str | None = None
    customer_id: str | None = None


class ContractTermIn(BaseModel):
    contract_start: str | None = None  # ISO date
    contract_term_months: int | None = Field(default=None, ge=1, le=240)
    contract_end: str | None = None  # ISO date; derived from start + term when omitted
    auto_renew: bool = False
    renewal_notice_days: int = Field(default=90, ge=0, le=365)


class InviteIn(BaseModel):
    email: str
    name: str | None = None


class FleetIn(BaseModel):
    """`null` clears the override and reverts to the derived vehicle count."""

    fleet_size: int | None = None


def _audit(repo: Repository, engagement_id: str, actor: Principal, action: str, **kw) -> None:
    repo.put_audit(
        AuditEvent(
            engagement_id=engagement_id, event_id=new_id(), ts=utcnow(),
            actor_id=actor.user_id, actor_role=actor.group_role.value, actor_name=actor.name,
            action=action, target=kw.get("target"), comment=kw.get("comment"),
        )
    )


@router.post("/engagements", status_code=201)
def create_engagement(
    body: CreateEngagementIn,
    principal: Principal = Depends(get_principal),
    repo: Repository = Depends(get_repo),
):
    if not principal.is_provider:
        raise HTTPException(403, "only provider-side users can create engagements")
    customer_id, client_name = body.customer_id, (body.client_name or "").strip()
    if customer_id:
        customer = repo.get_customer(customer_id)
        if customer is None:
            raise HTTPException(404, "customer not found")
        client_name = customer.legal_name
    elif not client_name:
        raise HTTPException(400, "customer_id or client_name is required")
    eid = new_id()
    engagement = repo.put_engagement(
        Engagement(
            engagement_id=eid, name=body.name, client_name=client_name,
            customer_id=customer_id,
            fleet_size_override=None, fleet_size=0,
            created_by=principal.user_id, created_at=utcnow(),
        )
    )
    repo.put_membership(
        Membership(
            engagement_id=eid, user_id=principal.user_id, email=principal.email,
            role=Role.PROVIDER, name=principal.name, created_at=utcnow(),
        )
    )
    sid = new_id()
    repo.put_submission(
        Submission(engagement_id=eid, submission_id=sid, created_at=utcnow(), updated_at=utcnow())
    )
    _audit(repo, eid, principal, "engagement_created")
    return {"engagement": engagement, "submission_id": sid}


@router.get("/engagements")
def list_engagements(
    principal: Principal = Depends(get_principal), repo: Repository = Depends(get_repo)
):
    # Provider-side staff are org-level and see every engagement; clients see only theirs.
    if principal.is_provider:
        engagements = repo.list_all_engagements()
    else:
        ids = repo.list_user_engagement_ids(principal.user_id)
        engagements = [e for e in (repo.get_engagement(i) for i in ids) if e is not None]
    engagements.sort(key=lambda e: e.created_at, reverse=True)
    return {"engagements": engagements}


@router.get("/engagements/{engagement_id}")
def get_engagement(
    engagement_id: str,
    member: Membership = Depends(membership_dep),
    repo: Repository = Depends(get_repo),
):
    engagement = repo.get_engagement(engagement_id)
    if engagement is None:
        raise HTTPException(404, "engagement not found")
    submissions = repo.list_submissions(engagement_id)
    sub = repo.current_submission(engagement_id)
    live = repo.live_submission(engagement_id)
    documents = []
    for d in repo.list_documents(engagement_id):
        fields = repo.list_fields(engagement_id, d.document_id, d.current_version)
        elected = [f for f in fields if f.elected]
        approved = sum(1 for f in elected if f.approved)
        item = d.model_dump(mode="json")
        # Whether this particular agreement still needs reading, so the interface can offer
        # extraction on the documents that need it rather than on the engagement as a whole.
        version = repo.get_document_version(engagement_id, d.document_id, d.current_version)
        item["extraction_status"] = version.status if version else "missing"
        item["needs_extraction"] = version is None or version.status != "extracted"
        item["review"] = {
            "approved": approved,
            "total": len(elected),
            "pct": round(100 * approved / len(elected)) if elected else 100,
        }
        documents.append(item)
    # With scope derived, "required" describes what the agreements in force amount to rather
    # than a checklist to satisfy; nothing is missing until a type is known and absent.
    required = required_doc_types(engagement.scope) if engagement.scope else []
    present = set(sub.docs()) if sub else set()
    assigned = repo.count_vehicles_for_engagement(engagement_id)
    return {
        "engagement": engagement,
        "customer": repo.get_customer(engagement.customer_id)
        if engagement.customer_id
        else None,
        "members": repo.list_members(engagement_id),
        "your_role": member.role,
        "submission": sub,
        "documents": documents,
        "required_doc_types": list(required),
        "missing_doc_types": [t for t in required if t not in present],
        "assigned_vehicle_count": assigned,
        "fleet_size_source": fleet_source(engagement.fleet_size_override),
        # An engagement can hold several review cycles: the agreements it started on, and one
        # per amendment since. `submission` above is the cycle in play; these say how it sits
        # among the others so the interface can show an amendment as a change to a live deal
        # rather than as the deal itself.
        "cycles": [
            {
                "submission_id": c.submission_id,
                "cycle": c.cycle,
                "label": cycle_label(c.cycle),
                "status": c.status.value,
                # The agreements this cycle settled on. For the live cycle these are the ones
                # billing, which is how an amendment can say which document still governs
                # rather than guessing from standing alone.
                "document_ids": c.docs(),
                "created_at": c.created_at,
            }
            for c in submissions
        ],
        "live_submission_id": live.submission_id if live else None,
        "is_amendment": bool(sub and sub.cycle > 1),
        "cycle_label": cycle_label(sub.cycle) if sub else None,
        # Only a provider may open one, and only on a cycle that has completed.
        "can_open_amendment": (
            member.role != Role.CLIENT and can_open_amendment(sub.status if sub else None)
        ),
    }


@router.patch("/engagements/{engagement_id}")
def update_engagement(
    engagement_id: str,
    body: UpdateEngagementIn,
    member: Membership = Depends(require_provider),
    principal: Principal = Depends(get_principal),
    repo: Repository = Depends(get_repo),
):
    """Rename an engagement, move it to another customer, or change its scope.

    Scope is not settable: it is derived from the agreements in force.
    """
    engagement = repo.get_engagement(engagement_id)
    if engagement is None:
        raise HTTPException(404, "engagement not found")
    if body.name:
        repo.set_engagement_name(engagement_id, body.name)
    if body.customer_id:
        customer = repo.get_customer(body.customer_id)
        if customer is None:
            raise HTTPException(404, "customer not found")
        repo.set_engagement_customer(engagement_id, body.customer_id, customer.legal_name)
    return {"engagement": repo.get_engagement(engagement_id)}


@router.get("/engagements/{engagement_id}/audit")
def get_audit(
    engagement_id: str,
    member: Membership = Depends(membership_dep),
    repo: Repository = Depends(get_repo),
):
    events = repo.list_audit(engagement_id)
    allowed = visible_actions(member.role == Role.CLIENT)
    if allowed is not None:
        events = [e for e in events if e.action in allowed]
    return {"events": events}


@router.get("/engagements/{engagement_id}/billing")
def get_billing(
    engagement_id: str,
    member: Membership = Depends(membership_dep),
    repo: Repository = Depends(get_repo),
    s3: S3Store = Depends(get_s3),
):
    engagement = repo.get_engagement(engagement_id)
    fleet_size = engagement.fleet_size if engagement else 100
    monthly_recurring = engagement.monthly_recurring if engagement else None
    sub = repo.billing_submission(engagement_id)
    if sub is None:
        return {"config": None, "status": None, "fleet_size": fleet_size}
    config = None
    try:
        import json

        from ..objects import billing_config_key

        raw = s3.get_bytes(billing_config_key(engagement_id, sub.submission_id))
        config = json.loads(raw)
    except Exception:  # noqa: BLE001 - config only exists once billing is set up
        config = None

    # Payment schedule (only once billing is active); backfill legacy engagements on read.
    schedule: list = []
    summary: dict = {}
    frequency = engagement.billing_frequency if engagement else "monthly"
    if config and engagement and sub.status.value in ("BILLING_SETUP", "ACTIVE"):
        import datetime

        from ..billing.config_builder import ensure_schedule
        from ..billing.estimate import compute_monthly_recurring
        from ..billing.schedule import enrich, summarize

        # Self-heal engagements set up before recurring dues were persisted.
        if engagement.monthly_recurring is None:
            repo.set_engagement_billing(
                engagement_id, engagement.fleet_size,
                compute_monthly_recurring(config, engagement.fleet_size),
            )
        payments = ensure_schedule(repo, engagement, sub.submission_id, config)
        engagement = repo.get_engagement(engagement_id)  # pick up billing_start/frequency/dues
        frequency = engagement.billing_frequency
        monthly_recurring = engagement.monthly_recurring
        fleet_size = engagement.fleet_size
        schedule = enrich(
            payments, engagement.monthly_recurring, frequency, datetime.date.today()
        )
        summary = summarize(schedule)
    return {
        "config": config,
        "status": sub.status.value,
        "signature": sub.client_signature,
        "fleet_size": fleet_size,
        "fleet_size_override": engagement.fleet_size_override if engagement else None,
        "assigned_vehicle_count": repo.count_vehicles_for_engagement(engagement_id),
        "fleet_size_source": fleet_source(
            engagement.fleet_size_override if engagement else None
        ),
        "monthly_recurring": monthly_recurring,
        "frequency": frequency,
        "schedule": schedule,
        "summary": summary,
    }


@router.post("/engagements/{engagement_id}/invitations", status_code=201)
def invite_user(
    engagement_id: str,
    body: InviteIn,
    member: Membership = Depends(require_provider),
    principal: Principal = Depends(get_principal),
    repo: Repository = Depends(get_repo),
):
    engagement = repo.get_engagement(engagement_id)
    if engagement is None:
        raise HTTPException(404, "engagement not found")
    from ..invites import create_and_invite

    try:
        # Engagement-level invites are always client reviewers; provider staff are org-level.
        invited = create_and_invite(
            repo, engagement, body.email, Role.CLIENT,
            inviter_name=principal.name or principal.email, name=body.name,
        )
    except Exception as e:  # noqa: BLE001 - surface Cognito/SES failures as a clean 400
        raise HTTPException(400, f"invite failed: {e}") from e
    _audit(repo, engagement_id, principal, "user_invited", target=body.email)
    return {"membership": invited}


@router.put("/engagements/{engagement_id}/contract")
def set_contract_term(
    engagement_id: str,
    body: ContractTermIn,
    member: Membership = Depends(require_provider),
    principal: Principal = Depends(get_principal),
    repo: Repository = Depends(get_repo),
):
    """Record when the master agreement expires, so renewals can be seen coming."""
    import datetime

    if repo.get_engagement(engagement_id) is None:
        raise HTTPException(404, "engagement not found")
    end = body.contract_end
    if end is None and body.contract_start and body.contract_term_months:
        try:
            start = datetime.date.fromisoformat(body.contract_start)
        except ValueError as e:
            raise HTTPException(400, "contract_start must be an ISO date") from e
        total = start.month - 1 + body.contract_term_months
        year, month = start.year + total // 12, total % 12 + 1
        import calendar

        day = min(start.day, calendar.monthrange(year, month)[1])
        end = datetime.date(year, month, day).isoformat()
    repo.set_engagement_contract(
        engagement_id, body.contract_start, body.contract_term_months, end,
        body.auto_renew, body.renewal_notice_days,
    )
    _audit(repo, engagement_id, principal, "contract_term_set", comment=end)
    return {"engagement": repo.get_engagement(engagement_id)}


@router.patch("/engagements/{engagement_id}/billing")
def update_billing_settings(
    engagement_id: str,
    body: FleetIn,
    member: Membership = Depends(require_provider),
    repo: Repository = Depends(get_repo),
    s3: S3Store = Depends(get_s3),
):
    """Override the fleet size, or clear the override to fall back to the vehicle count.

    Fleet size is finalized during the approval stages; once billing is active it is locked,
    because the payment schedule recomputes unpaid installments from the current dues.
    """
    engagement = repo.get_engagement(engagement_id)
    if engagement is None:
        raise HTTPException(404, "engagement not found")
    sub = repo.billing_submission(engagement_id)
    if sub and is_locked(sub.status.value):
        raise HTTPException(409, "fleet size is locked once billing is active")
    repo.set_fleet_override(engagement_id, body.fleet_size)
    assigned = repo.count_vehicles_for_engagement(engagement_id)
    fleet = effective_fleet_size(assigned, body.fleet_size)
    monthly = None
    if sub is not None:
        import json

        from ..billing.estimate import compute_monthly_recurring
        from ..objects import billing_config_key

        try:
            raw = s3.get_bytes(billing_config_key(engagement_id, sub.submission_id))
            monthly = compute_monthly_recurring(json.loads(raw), fleet)
        except Exception:  # noqa: BLE001 - config only exists once billing is set up
            monthly = None
    repo.set_engagement_billing(engagement_id, fleet, monthly)
    return {
        "fleet_size": fleet,
        "fleet_size_override": body.fleet_size,
        "assigned_vehicle_count": assigned,
        "fleet_size_source": fleet_source(body.fleet_size),
        "monthly_recurring": monthly,
    }
