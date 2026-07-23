"""Review insight endpoints: change verification (provider) + negotiation summary (both)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..auth.deps import get_repo, membership_dep, require_provider
from ..lifecycle.submission_state import Role
from ..review.insights import build_change_review, build_summary
from ..store.models import Membership
from ..store.repository import Repository

router = APIRouter(tags=["insights"])


@router.get("/engagements/{engagement_id}/submissions/{submission_id}/change-review")
def change_review(
    engagement_id: str,
    submission_id: str,
    member: Membership = Depends(require_provider),
    repo: Repository = Depends(get_repo),
):
    sub = repo.get_submission(engagement_id, submission_id)
    if sub is None:
        raise HTTPException(404, "submission not found")
    return build_change_review(repo, sub)


@router.get("/engagements/{engagement_id}/summary")
def negotiation_summary(
    engagement_id: str,
    member: Membership = Depends(membership_dep),
    repo: Repository = Depends(get_repo),
):
    subs = repo.list_submissions(engagement_id)
    if not subs:
        return {"summary": "", "highlights": [], "thread": []}
    return build_summary(repo, subs[0], for_client=member.role == Role.CLIENT)
