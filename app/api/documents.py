"""Documents: presigned upload, page renders (left panel), field review + approve/correct."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth.deps import get_principal, get_repo, get_s3, membership_dep, require_provider
from ..auth.principal import Principal
from ..lifecycle.submission_state import Role
from ..objects import page_key, source_pdf_key
from ..store.models import AuditEvent, Document, DocumentVersion, Membership
from ..store.repository import Repository, new_id, utcnow
from ..store.s3 import S3Store
from ..validation.reconcile import reconcile_engagement

router = APIRouter(tags=["documents"])


class PresignIn(BaseModel):
    filename: str
    submission_id: str
    # Uploaders no longer pick a type; the parse worker reads it off the text. These two exist
    # for the replace flow (a new version of an existing agreement) and a manual correction.
    document_id: str | None = None
    doc_type: str | None = None


class DocTypeIn(BaseModel):
    doc_type: str  # MSA | MLA


class FieldPatchIn(BaseModel):
    approved: bool | None = None
    fee_items: list[dict[str, Any]] | None = None
    notes: str | None = None


# When an upload can join the cycle in play. Mid-review the terms are with the customer or
# finance and a document arriving underneath them would change what they are looking at; once
# the cycle has completed, the deal is signed and billing, so a new agreement belongs to an
# amendment rather than to the cycle that closed.
_UPLOADABLE_STATUSES = {
    "DRAFT", "IN_UNDERWRITING", "VALIDATION_FAILED",
    "CHANGES_REQUESTED_CLIENT", "CHANGES_REQUESTED_FINANCE",
}


@router.post("/engagements/{engagement_id}/documents:presign", status_code=201)
def presign_upload(
    engagement_id: str,
    body: PresignIn,
    member: Membership = Depends(require_provider),
    repo: Repository = Depends(get_repo),
    s3: S3Store = Depends(get_s3),
):
    if body.doc_type is not None and body.doc_type not in ("MSA", "MLA"):
        raise HTTPException(400, "doc_type must be MSA or MLA when given")

    current = repo.current_submission(engagement_id)
    status = current.status.value if current else "DRAFT"
    if status not in _UPLOADABLE_STATUSES:
        detail = (
            "this engagement is live — open an amendment to add or renew an agreement"
            if status == "ACTIVE"
            else f"agreements cannot be uploaded while the submission is {status}"
        )
        raise HTTPException(409, detail)

    # An engagement can hold several agreements of the same type over its life — the one in
    # force plus superseded ones kept for the record — so an upload creates a new document
    # unless it explicitly replaces an existing one.
    existing = repo.get_document(engagement_id, body.document_id) if body.document_id else None
    if body.document_id and existing is None:
        raise HTTPException(404, "document not found")
    if existing is not None:
        document_id = existing.document_id
        version = existing.current_version + 1
        existing.current_version = version
        existing.filename = body.filename
        repo.put_document(existing)
    else:
        document_id = new_id()
        version = 1
        repo.put_document(
            Document(
                engagement_id=engagement_id, document_id=document_id,
                doc_type=body.doc_type or "UNKNOWN",
                type_overridden=body.doc_type is not None,
                filename=body.filename, current_version=version,
                cycle=current.cycle if current else 1, created_at=utcnow(),
            )
        )
        # Standing and the submission's slots are settled once the type is known; if the
        # uploader supplied one, that is now.
        if body.doc_type is not None:
            reconcile_engagement(repo, engagement_id)

    key = source_pdf_key(engagement_id, document_id, version)
    repo.put_document_version(
        DocumentVersion(
            engagement_id=engagement_id, document_id=document_id, version=version,
            s3_key=key, uploaded_at=utcnow(),
        )
    )
    return {
        "document_id": document_id,
        "version": version,
        "s3_key": key,
        "upload_url": s3.presign_put(key, content_type="application/pdf"),
    }


@router.put("/engagements/{engagement_id}/documents/{document_id}/type")
def set_document_type(
    engagement_id: str,
    document_id: str,
    body: DocTypeIn,
    member: Membership = Depends(require_provider),
    repo: Repository = Depends(get_repo),
):
    """Correct a misclassified agreement.

    Classification reads the document's own text, which is reliable but not infallible — a
    scanned cover page or an unusual title can defeat it. An explicit correction sticks: the
    parse worker will not overwrite it on a later re-upload.
    """
    if body.doc_type not in ("MSA", "MLA"):
        raise HTTPException(400, "doc_type must be MSA or MLA")
    if repo.get_document(engagement_id, document_id) is None:
        raise HTTPException(404, "document not found")
    repo.set_document_meta(
        engagement_id, document_id, doc_type=body.doc_type, type_overridden=True
    )
    result = reconcile_engagement(repo, engagement_id)
    return {"ok": True, "scope": result["scope"]}


# Once the terms are with the customer or finance, removing an agreement would pull the
# ground out from under what they are reviewing. Before that, an upload is still a working
# document and a wrong file should be removable outright.
_DELETABLE_STATUSES = {
    "DRAFT", "IN_UNDERWRITING", "VALIDATION_FAILED", "CHANGES_REQUESTED_CLIENT",
}


@router.delete("/engagements/{engagement_id}/documents/{document_id}", status_code=200)
def delete_document(
    engagement_id: str,
    document_id: str,
    member: Membership = Depends(require_provider),
    principal: Principal = Depends(get_principal),
    repo: Repository = Depends(get_repo),
    s3: S3Store = Depends(get_s3),
):
    """Remove an agreement uploaded in error, along with everything derived from it."""
    doc = repo.get_document(engagement_id, document_id)
    if doc is None:
        raise HTTPException(404, "document not found")

    current = repo.current_submission(engagement_id)
    status = current.status.value if current else "DRAFT"
    if status not in _DELETABLE_STATUSES:
        raise HTTPException(
            409, f"agreements cannot be removed while the submission is {status}"
        )

    removed = repo.delete_document(engagement_id, document_id)
    # The rendered pages, the source PDF and the extraction all live under this prefix.
    s3.delete_prefix(f"{engagement_id}/{document_id}/")
    # Standing, scope and the submission's slots all shift when an agreement leaves.
    result = reconcile_engagement(repo, engagement_id)
    repo.put_audit(
        AuditEvent(
            engagement_id=engagement_id, event_id=new_id(), ts=utcnow(),
            actor_id=principal.user_id, actor_role=principal.group_role.value,
            actor_name=principal.name, action="document_removed", target=doc.filename,
        )
    )
    return {"ok": True, "items_removed": removed, "scope": result["scope"]}


@router.get("/engagements/{engagement_id}/documents")
def list_documents(
    engagement_id: str,
    member: Membership = Depends(membership_dep),
    repo: Repository = Depends(get_repo),
):
    return {"documents": repo.list_documents(engagement_id)}


@router.get("/engagements/{engagement_id}/documents/{document_id}")
def get_document(
    engagement_id: str,
    document_id: str,
    member: Membership = Depends(membership_dep),
    repo: Repository = Depends(get_repo),
):
    doc = repo.get_document(engagement_id, document_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    version = repo.get_document_version(engagement_id, document_id, doc.current_version)
    return {"document": doc, "version": version}


@router.get("/engagements/{engagement_id}/documents/{document_id}/versions/{version}/pages")
def get_pages(
    engagement_id: str,
    document_id: str,
    version: int,
    member: Membership = Depends(membership_dep),
    repo: Repository = Depends(get_repo),
    s3: S3Store = Depends(get_s3),
):
    v = repo.get_document_version(engagement_id, document_id, version)
    if v is None:
        raise HTTPException(404, "document version not found")
    pages = [
        {"page": p, "image_url": s3.presign_get(page_key(engagement_id, document_id, version, p))}
        for p in range(1, (v.page_count or 0) + 1)
    ]
    return {"page_count": v.page_count or 0, "pages": pages}


@router.get("/engagements/{engagement_id}/documents/{document_id}/versions/{version}/fields")
def get_fields(
    engagement_id: str,
    document_id: str,
    version: int,
    member: Membership = Depends(membership_dep),
    repo: Repository = Depends(get_repo),
):
    fields = repo.list_fields(engagement_id, document_id, version)
    # Client-side users only ever see approved terms.
    if member.role == Role.CLIENT:
        fields = [f for f in fields if f.approved]
    return {"fields": fields, "needs_review_count": sum(1 for f in fields if f.needs_review)}


@router.patch(
    "/engagements/{engagement_id}/documents/{document_id}/versions/{version}"
    "/fields/{service}/{field_id}"
)
def patch_field(
    engagement_id: str,
    document_id: str,
    version: int,
    service: str,
    field_id: str,
    body: FieldPatchIn,
    member: Membership = Depends(require_provider),
    principal: Principal = Depends(get_principal),
    repo: Repository = Depends(get_repo),
):
    updates: dict[str, Any] = {}
    action = "field_reviewed"
    if body.fee_items is not None:
        updates["fee_items"] = body.fee_items
        updates["corrected"] = True
        action = "field_corrected"
    if body.notes is not None:
        updates["notes"] = body.notes
    if body.approved:
        updates["approved"] = True
        updates["needs_review"] = False
        action = "field_approved" if action == "field_reviewed" else action
    if not updates:
        raise HTTPException(400, "no changes provided")
    repo.update_field(engagement_id, document_id, version, service, field_id, updates)
    repo.put_audit(
        AuditEvent(
            engagement_id=engagement_id, event_id=new_id(), ts=utcnow(),
            actor_id=principal.user_id, actor_role=member.role.value,
            actor_name=member.name or principal.name, action=action,
            target=f"{service}/{field_id}",
        )
    )
    return {"ok": True, "action": action}
