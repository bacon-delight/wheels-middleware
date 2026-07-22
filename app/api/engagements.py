"""Engagements, membership, invitations, audit trail."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth.deps import get_principal, get_repo, membership_dep, require_provider
from ..auth.principal import Principal
from ..lifecycle.submission_state import Role
from ..store.models import AuditEvent, Engagement, Membership, Submission
from ..store.repository import Repository, new_id, utcnow

router = APIRouter(tags=["engagements"])


class CreateEngagementIn(BaseModel):
    name: str
    client_name: str


class InviteIn(BaseModel):
    email: str
    role: Role
    name: str | None = None


def _audit(repo: Repository, engagement_id: str, actor: Principal, action: str, **kw) -> None:
    repo.put_audit(
        AuditEvent(
            engagement_id=engagement_id, event_id=new_id(), ts=utcnow(),
            actor_id=actor.user_id, actor_role=actor.group_role.value, action=action,
            target=kw.get("target"), comment=kw.get("comment"),
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
    ids = repo.list_user_engagement_ids(principal.user_id)
    engagements = [e for e in (repo.get_engagement(i) for i in ids) if e is not None]
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
    return {
        "engagement": engagement,
        "members": repo.list_members(engagement_id),
        "your_role": member.role,
        "submission": submissions[0] if submissions else None,
        "documents": repo.list_documents(engagement_id),
    }


@router.get("/engagements/{engagement_id}/audit")
def get_audit(
    engagement_id: str,
    member: Membership = Depends(membership_dep),
    repo: Repository = Depends(get_repo),
):
    return {"events": repo.list_audit(engagement_id)}


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
        invited = create_and_invite(
            repo, engagement, body.email, body.role,
            inviter_name=principal.name or principal.email, name=body.name,
        )
    except Exception as e:  # noqa: BLE001 - surface Cognito/SES failures as a clean 400
        raise HTTPException(400, f"invite failed: {e}") from e
    _audit(repo, engagement_id, principal, "user_invited", target=body.email)
    return {"membership": invited}
