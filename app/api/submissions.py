"""Submission lifecycle: read status + drive the guarded state-machine transitions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth.deps import get_principal, get_repo, get_s3, membership_dep
from ..auth.principal import Principal
from ..lifecycle.submission_state import (
    Action,
    IllegalTransition,
    Role,
    SubmissionStatus,
    allowed_actions,
    transition,
)
from ..messaging import emit_lifecycle_event, enqueue_ingest
from ..store.models import AuditEvent, Membership, Submission
from ..store.repository import ConflictError, Repository, new_id, utcnow
from ..store.s3 import S3Store

router = APIRouter(tags=["submissions"])


class CommentIn(BaseModel):
    comment: str = ""


class SignatureIn(BaseModel):
    full_name: str
    place: str = ""


class ApproveIn(BaseModel):
    signature: SignatureIn


def _apply(
    repo: Repository, principal: Principal, member: Membership, sub: Submission,
    action: Action, comment: str | None = None,
) -> Submission:
    """Validate + persist one guarded transition, audit it, and emit a lifecycle event."""
    try:
        target = transition(sub.status, action, member.role)
    except IllegalTransition as e:
        raise HTTPException(409, detail=str(e)) from e
    extra = {"latest_comment": comment} if comment else None
    try:
        updated = repo.update_submission_status(
            sub.engagement_id, sub.submission_id, sub.status.value, target.value, extra=extra
        )
    except ConflictError as e:
        raise HTTPException(409, detail=str(e)) from e
    repo.put_audit(
        AuditEvent(
            engagement_id=sub.engagement_id, event_id=new_id(), ts=utcnow(),
            actor_id=principal.user_id, actor_role=member.role.value,
            actor_name=member.name or principal.name,
            action=action.value, target=sub.submission_id, comment=comment,
        )
    )
    emit_lifecycle_event(
        action.value,
        {
            "engagement_id": sub.engagement_id, "submission_id": sub.submission_id,
            "status": updated.status.value, "actor_role": member.role.value,
            "actor_name": member.name or principal.name, "comment": comment,
        },
    )
    return updated


def _load(repo: Repository, engagement_id: str, submission_id: str) -> Submission:
    sub = repo.get_submission(engagement_id, submission_id)
    if sub is None:
        raise HTTPException(404, "submission not found")
    return sub


def _system(engagement_id: str) -> Membership:
    """A synthetic SYSTEM actor for machine transitions (capture-fields, billing-done)."""
    return Membership(
        engagement_id=engagement_id, user_id="system", email="",
        role=Role.SYSTEM, created_at=utcnow(),
    )


@router.get("/engagements/{engagement_id}/submissions/{submission_id}")
def get_submission(
    engagement_id: str,
    submission_id: str,
    member: Membership = Depends(membership_dep),
    repo: Repository = Depends(get_repo),
):
    sub = _load(repo, engagement_id, submission_id)
    return {
        "submission": sub,
        "your_role": member.role,
        "allowed_actions": [a.value for a in allowed_actions(sub.status, member.role)],
    }


@router.post("/engagements/{engagement_id}/submissions/{submission_id}:submit-for-processing")
def submit_for_processing(
    engagement_id: str,
    submission_id: str,
    member: Membership = Depends(membership_dep),
    principal: Principal = Depends(get_principal),
    repo: Repository = Depends(get_repo),
):
    sub = _load(repo, engagement_id, submission_id)
    updated = _apply(repo, principal, member, sub, Action.SUBMIT_FOR_PROCESSING)
    # Kick off the pipeline for each uploaded document in the submission.
    for document_id in (sub.msa_document_id, sub.mla_document_id):
        if not document_id:
            continue
        doc = repo.get_document(engagement_id, document_id)
        ver = (
            repo.get_document_version(engagement_id, document_id, doc.current_version)
            if doc
            else None
        )
        if doc and ver:
            enqueue_ingest(
                {
                    "engagement_id": engagement_id, "submission_id": submission_id,
                    "document_id": document_id, "version": ver.version,
                    "s3_key": ver.s3_key, "doc_type": doc.doc_type,
                }
            )
    return {"submission": updated}


@router.post("/engagements/{engagement_id}/submissions/{submission_id}:submit-to-client")
def submit_to_client(
    engagement_id: str, submission_id: str,
    member: Membership = Depends(membership_dep),
    principal: Principal = Depends(get_principal), repo: Repository = Depends(get_repo),
):
    sub = _load(repo, engagement_id, submission_id)
    return {"submission": _apply(repo, principal, member, sub, Action.SUBMIT_TO_CLIENT)}


@router.post("/engagements/{engagement_id}/submissions/{submission_id}:client-approve")
def client_approve(
    engagement_id: str, submission_id: str, body: ApproveIn,
    member: Membership = Depends(membership_dep),
    principal: Principal = Depends(get_principal), repo: Repository = Depends(get_repo),
):
    full_name = body.signature.full_name.strip()
    if not full_name:
        raise HTTPException(400, "a full-name signature is required to approve")
    sub = _load(repo, engagement_id, submission_id)
    # Capture the digital signature (typed full name acts as the signature) with date + place.
    place = body.signature.place.strip()
    sub.client_signature = {"full_name": full_name, "place": place, "signed_at": utcnow()}
    repo.put_submission(sub)
    signed = f"Digitally signed by {full_name}" + (f" at {place}" if place else "")
    _apply(repo, principal, member, sub, Action.CLIENT_APPROVE, comment=signed)
    # System step: freeze the approved terms and move to finance review.
    sub = _load(repo, engagement_id, submission_id)
    system = _system(engagement_id)
    updated = _apply(repo, principal, system, sub, Action.CAPTURE_FIELDS)
    return {"submission": updated}


@router.post("/engagements/{engagement_id}/submissions/{submission_id}:resubmit-to-client")
def resubmit_to_client(
    engagement_id: str, submission_id: str, body: CommentIn,
    member: Membership = Depends(membership_dep),
    principal: Principal = Depends(get_principal), repo: Repository = Depends(get_repo),
):
    """Provider declines the client's change request and resubmits the terms with a response."""
    sub = _load(repo, engagement_id, submission_id)
    updated = _apply(repo, principal, member, sub, Action.RESUBMIT_TO_CLIENT, body.comment)
    return {"submission": updated}


@router.post("/engagements/{engagement_id}/submissions/{submission_id}:reupload")
def reupload(
    engagement_id: str, submission_id: str, body: CommentIn,
    member: Membership = Depends(membership_dep),
    principal: Principal = Depends(get_principal), repo: Repository = Depends(get_repo),
):
    """Provider re-uploaded document(s) with the requested change; re-validate + re-extract."""
    sub = _load(repo, engagement_id, submission_id)
    updated = _apply(repo, principal, member, sub, Action.REUPLOAD, body.comment)
    for document_id in (sub.msa_document_id, sub.mla_document_id):
        if not document_id:
            continue
        doc = repo.get_document(engagement_id, document_id)
        ver = (
            repo.get_document_version(engagement_id, document_id, doc.current_version)
            if doc
            else None
        )
        if doc and ver:
            enqueue_ingest({
                "engagement_id": engagement_id, "submission_id": submission_id,
                "document_id": document_id, "version": ver.version,
                "s3_key": ver.s3_key, "doc_type": doc.doc_type,
            })
    return {"submission": updated}


@router.post("/engagements/{engagement_id}/submissions/{submission_id}:client-request-changes")
def client_request_changes(
    engagement_id: str, submission_id: str, body: CommentIn,
    member: Membership = Depends(membership_dep),
    principal: Principal = Depends(get_principal), repo: Repository = Depends(get_repo),
):
    sub = _load(repo, engagement_id, submission_id)
    updated = _apply(repo, principal, member, sub, Action.CLIENT_REQUEST_CHANGES, body.comment)
    return {"submission": updated}


@router.post("/engagements/{engagement_id}/submissions/{submission_id}:finance-approve")
def finance_approve(
    engagement_id: str, submission_id: str,
    member: Membership = Depends(membership_dep),
    principal: Principal = Depends(get_principal), repo: Repository = Depends(get_repo),
):
    sub = _load(repo, engagement_id, submission_id)
    return {"submission": _apply(repo, principal, member, sub, Action.FINANCE_APPROVE)}


@router.post("/engagements/{engagement_id}/submissions/{submission_id}:finance-request-changes")
def finance_request_changes(
    engagement_id: str, submission_id: str, body: CommentIn,
    member: Membership = Depends(membership_dep),
    principal: Principal = Depends(get_principal), repo: Repository = Depends(get_repo),
):
    sub = _load(repo, engagement_id, submission_id)
    return {
        "submission": _apply(
            repo, principal, member, sub, Action.FINANCE_REQUEST_CHANGES, body.comment
        )
    }


@router.post("/engagements/{engagement_id}/submissions/{submission_id}:setup-billing")
def setup_billing(
    engagement_id: str, submission_id: str,
    member: Membership = Depends(membership_dep),
    principal: Principal = Depends(get_principal), repo: Repository = Depends(get_repo),
    s3: S3Store = Depends(get_s3),
):
    sub = _load(repo, engagement_id, submission_id)
    _apply(repo, principal, member, sub, Action.SETUP_BILLING)
    from ..billing.config_builder import build_billing_config

    build_billing_config(repo, engagement_id, submission_id, s3=s3)
    sub = _load(repo, engagement_id, submission_id)
    system = _system(engagement_id)
    updated = _apply(repo, principal, system, sub, Action.BILLING_DONE)
    return {"submission": updated, "status": SubmissionStatus.ACTIVE.value}
