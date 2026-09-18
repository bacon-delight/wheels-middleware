"""Submission lifecycle: read status + drive the guarded state-machine transitions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth.deps import get_principal, get_repo, get_s3, membership_dep, require_provider
from ..auth.principal import Principal
from ..lifecycle.amendment import can_open_amendment, cycle_label
from ..lifecycle.submission_state import (
    Action,
    IllegalTransition,
    Role,
    SubmissionStatus,
    allowed_actions,
    transition,
)
from ..messaging import emit_lifecycle_event, enqueue_ingest
from ..store.models import AuditEvent, DocumentStanding, Membership, Submission
from ..store.repository import ConflictError, Repository, new_id, utcnow
from ..store.s3 import S3Store
from ..validation.reconcile import pending_documents

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
    extra = (
        {"latest_comment": comment, "latest_comment_by": member.name or principal.name}
        if comment
        else None
    )
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
    # The only data-completeness gate in the flow: the state machine stays pure (source,
    # action, role). The gate counts uploaded documents, not the submission's typed slots —
    # classification happens inside this run, so requiring a classified document first would
    # mean nothing could ever be classified.
    if not [d for d in repo.list_documents(engagement_id)
            if d.standing != DocumentStanding.SUPERSEDED.value]:
        raise HTTPException(400, "upload at least one agreement before running extraction")
    documents = pending_documents(repo, engagement_id)
    if not documents:
        raise HTTPException(409, "every agreement has already been extracted")
    updated = _apply(repo, principal, member, sub, Action.SUBMIT_FOR_PROCESSING)
    # Only the documents nothing has read yet, including ones whose type is not known:
    # parsing is what reads the type off the text.
    for doc in documents:
        ver = repo.get_document_version(engagement_id, doc.document_id, doc.current_version)
        if ver:
            enqueue_ingest(
                {
                    "engagement_id": engagement_id, "submission_id": submission_id,
                    "document_id": doc.document_id, "version": ver.version,
                    "s3_key": ver.s3_key, "doc_type": doc.doc_type,
                }
            )
    return {"submission": updated}


@router.post("/engagements/{engagement_id}/amendments", status_code=201)
def open_amendment(
    engagement_id: str,
    member: Membership = Depends(require_provider),
    principal: Principal = Depends(get_principal),
    repo: Repository = Depends(get_repo),
):
    """Open a further review cycle on a live engagement.

    A renewal, an added lease or service, or a reissued document all change the terms, and
    changed terms need the customer's and finance's approval before they can be billed. That
    is the cycle this opens: a new submission starting at DRAFT, carrying forward the
    agreements in force so the amendment begins from what the parties actually signed.

    The completed cycle is left exactly as it is. It keeps its signature, its approved terms
    and its billing configuration, and it keeps the engagement ACTIVE while the amendment is
    reviewed, so nothing about the live deal changes on the strength of an unapproved
    document.
    """
    current = repo.current_submission(engagement_id)
    if current is None:
        raise HTTPException(404, "submission not found")
    if not can_open_amendment(current.status):
        raise HTTPException(
            409,
            "this engagement already has a review cycle in progress — upload the document to "
            "that cycle instead",
        )

    cycle = max((s.cycle for s in repo.list_submissions(engagement_id)), default=1) + 1
    amendment = repo.put_submission(
        Submission(
            engagement_id=engagement_id,
            submission_id=new_id(),
            status=SubmissionStatus.DRAFT,
            cycle=cycle,
            # Start from the agreements in force. An amendment changes some of them; the rest
            # carry over, and reconciliation re-derives the slots as documents are classified.
            document_ids=current.docs(),
            msa_document_id=current.msa_document_id,
            mla_document_id=current.mla_document_id,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
    )
    repo.put_audit(
        AuditEvent(
            engagement_id=engagement_id, event_id=new_id(), ts=utcnow(),
            actor_id=principal.user_id, actor_role=member.role.value,
            actor_name=member.name or principal.name,
            action="amendment_opened", target=cycle_label(cycle),
        )
    )
    return {"submission": amendment, "cycle": cycle, "label": cycle_label(cycle)}


@router.delete("/engagements/{engagement_id}/amendments/{submission_id}", status_code=200)
def discard_amendment(
    engagement_id: str,
    submission_id: str,
    member: Membership = Depends(require_provider),
    principal: Principal = Depends(get_principal),
    repo: Repository = Depends(get_repo),
):
    """Close an amendment that was opened by mistake, before any work went into it.

    Without this an accidental click would block the engagement for good: a cycle is open, so
    no second amendment may start, and the open cycle never completes. Only an untouched one
    can be discarded — once a document has been uploaded to it there is something to keep or
    to remove deliberately, and once it has left DRAFT the customer or finance may have seen
    it. The cycle already in force is never a candidate.
    """
    sub = repo.get_submission(engagement_id, submission_id)
    if sub is None:
        raise HTTPException(404, "submission not found")
    if sub.cycle <= 1:
        raise HTTPException(409, "the original review cycle cannot be discarded")
    if sub.status != SubmissionStatus.DRAFT:
        raise HTTPException(
            409,
            "an amendment can only be discarded before it starts, "
            f"not while {sub.status.value}",
        )
    own = [d for d in repo.list_documents(engagement_id) if d.cycle == sub.cycle]
    if own:
        raise HTTPException(
            409,
            "remove the agreements uploaded to this amendment before discarding it",
        )

    repo.delete_submission(engagement_id, submission_id)
    # The engagement's status followed the live cycle throughout, so nothing needs restoring.
    repo.put_audit(
        AuditEvent(
            engagement_id=engagement_id, event_id=new_id(), ts=utcnow(),
            actor_id=principal.user_id, actor_role=member.role.value,
            actor_name=member.name or principal.name,
            action="amendment_discarded", target=cycle_label(sub.cycle),
        )
    )
    return {"ok": True, "discarded_cycle": sub.cycle}


@router.post("/engagements/{engagement_id}/documents/{document_id}:extract")
def extract_document(
    engagement_id: str,
    document_id: str,
    member: Membership = Depends(require_provider),
    principal: Principal = Depends(get_principal),
    repo: Repository = Depends(get_repo),
):
    """Run extraction on one agreement.

    Extraction is per document, not per engagement: a newly uploaded or replaced agreement is
    the only thing that needs reading, and re-running the ones already extracted would spend a
    model call to reproduce terms an analyst may have already corrected and approved.

    The submission still moves as a whole, because the lifecycle is a property of the
    negotiation rather than of any one file. From DRAFT that is the first processing run; from
    underwriting it is a re-validation, which is the same path a corrected re-upload takes.
    """
    doc = repo.get_document(engagement_id, document_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    version = repo.get_document_version(engagement_id, document_id, doc.current_version)
    if version is None:
        raise HTTPException(409, "this document has no uploaded file yet")
    if version.status == "extracted":
        raise HTTPException(409, "this agreement has already been extracted")

    sub = repo.current_submission(engagement_id)
    if sub is None:
        raise HTTPException(404, "submission not found")
    status = sub.status.value
    if status == SubmissionStatus.DRAFT.value:
        sub = _apply(repo, principal, member, sub, Action.SUBMIT_FOR_PROCESSING)
    elif status in (
        SubmissionStatus.IN_UNDERWRITING.value,
        SubmissionStatus.VALIDATION_FAILED.value,
        SubmissionStatus.CHANGES_REQUESTED_CLIENT.value,
    ):
        sub = _apply(repo, principal, member, sub, Action.REUPLOAD)
    elif status not in (
        SubmissionStatus.EXTRACTING.value,
        SubmissionStatus.REVALIDATING.value,
    ):
        # Past underwriting the terms are with the customer or finance; re-reading a document
        # underneath them would change what they are looking at.
        raise HTTPException(409, f"cannot extract while the submission is {status}")

    enqueue_ingest({
        "engagement_id": engagement_id, "submission_id": sub.submission_id,
        "document_id": document_id, "version": version.version,
        "s3_key": version.s3_key, "doc_type": doc.doc_type,
    })
    return {"submission": sub, "document_id": document_id}


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
    # Same rule as the first run: only what has not been read at its current version.
    for doc in pending_documents(repo, engagement_id):
        ver = repo.get_document_version(engagement_id, doc.document_id, doc.current_version)
        if ver:
            enqueue_ingest({
                "engagement_id": engagement_id, "submission_id": submission_id,
                "document_id": doc.document_id, "version": ver.version,
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
    from ..billing.config_builder import build_billing_config, ensure_schedule
    from ..billing.estimate import compute_monthly_recurring

    config = build_billing_config(repo, engagement_id, submission_id, s3=s3)
    # Compute recurring dues at the engagement's fleet size for the finance dashboard + client.
    engagement = repo.get_engagement(engagement_id)
    fleet = engagement.fleet_size if engagement else 100
    repo.set_engagement_billing(
        engagement_id, fleet, compute_monthly_recurring(config, fleet)
    )
    # Build the dated payment schedule (initial + recurring) so the client can start paying.
    engagement = repo.get_engagement(engagement_id)
    if engagement:
        ensure_schedule(repo, engagement, submission_id, config)
    sub = _load(repo, engagement_id, submission_id)
    system = _system(engagement_id)
    updated = _apply(repo, principal, system, sub, Action.BILLING_DONE)
    return {"submission": updated, "status": SubmissionStatus.ACTIVE.value}
