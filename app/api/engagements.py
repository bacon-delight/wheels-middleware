"""Engagements, membership, invitations, audit trail."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth.deps import get_principal, get_repo, get_s3, membership_dep, require_provider
from ..auth.principal import Principal
from ..lifecycle.submission_state import Role
from ..store.models import AuditEvent, Engagement, Membership, Submission
from ..store.repository import Repository, new_id, utcnow
from ..store.s3 import S3Store

router = APIRouter(tags=["engagements"])


class CreateEngagementIn(BaseModel):
    name: str
    client_name: str


class InviteIn(BaseModel):
    email: str
    name: str | None = None


class FleetIn(BaseModel):
    fleet_size: int


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
    eid = new_id()
    engagement = repo.put_engagement(
        Engagement(
            engagement_id=eid, name=body.name, client_name=body.client_name,
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
    documents = []
    for d in repo.list_documents(engagement_id):
        fields = repo.list_fields(engagement_id, d.document_id, d.current_version)
        elected = [f for f in fields if f.elected]
        approved = sum(1 for f in elected if f.approved)
        item = d.model_dump(mode="json")
        item["review"] = {
            "approved": approved,
            "total": len(elected),
            "pct": round(100 * approved / len(elected)) if elected else 100,
        }
        documents.append(item)
    return {
        "engagement": engagement,
        "members": repo.list_members(engagement_id),
        "your_role": member.role,
        "submission": submissions[0] if submissions else None,
        "documents": documents,
    }


@router.get("/engagements/{engagement_id}/audit")
def get_audit(
    engagement_id: str,
    member: Membership = Depends(membership_dep),
    repo: Repository = Depends(get_repo),
):
    return {"events": repo.list_audit(engagement_id)}


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
    subs = repo.list_submissions(engagement_id)
    sub = subs[0] if subs else None
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


@router.patch("/engagements/{engagement_id}/billing")
def update_billing_settings(
    engagement_id: str,
    body: FleetIn,
    member: Membership = Depends(require_provider),
    repo: Repository = Depends(get_repo),
    s3: S3Store = Depends(get_s3),
):
    """Set the fleet size and recompute recurring dues from the generated billing config.

    Fleet size is finalized during the approval stages; once billing is active it is locked.
    """
    engagement = repo.get_engagement(engagement_id)
    if engagement is None:
        raise HTTPException(404, "engagement not found")
    subs = repo.list_submissions(engagement_id)
    sub = subs[0] if subs else None
    if sub and sub.status.value in ("BILLING_SETUP", "ACTIVE"):
        raise HTTPException(409, "fleet size is locked once billing is active")
    fleet = max(1, body.fleet_size)
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
    return {"fleet_size": fleet, "monthly_recurring": monthly}
