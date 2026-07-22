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

router = APIRouter(tags=["documents"])


class PresignIn(BaseModel):
    doc_type: str  # MSA | MLA
    filename: str
    submission_id: str


class FieldPatchIn(BaseModel):
    approved: bool | None = None
    fee_items: list[dict[str, Any]] | None = None
    notes: str | None = None


@router.post("/engagements/{engagement_id}/documents:presign", status_code=201)
def presign_upload(
    engagement_id: str,
    body: PresignIn,
    member: Membership = Depends(require_provider),
    repo: Repository = Depends(get_repo),
    s3: S3Store = Depends(get_s3),
):
    if body.doc_type not in ("MSA", "MLA"):
        raise HTTPException(400, "doc_type must be MSA or MLA")
    document_id = new_id()
    version = 1
    key = source_pdf_key(engagement_id, document_id, version)
    repo.put_document(
        Document(
            engagement_id=engagement_id, document_id=document_id, doc_type=body.doc_type,
            filename=body.filename, current_version=version, created_at=utcnow(),
        )
    )
    repo.put_document_version(
        DocumentVersion(
            engagement_id=engagement_id, document_id=document_id, version=version,
            s3_key=key, uploaded_at=utcnow(),
        )
    )
    # Link the document to its submission slot (MSA/MLA).
    sub = repo.get_submission(engagement_id, body.submission_id)
    if sub is not None:
        if body.doc_type == "MSA":
            sub.msa_document_id = document_id
        else:
            sub.mla_document_id = document_id
        sub.updated_at = utcnow()
        repo.put_submission(sub)
    return {
        "document_id": document_id,
        "version": version,
        "s3_key": key,
        "upload_url": s3.presign_put(key, content_type="application/pdf"),
    }


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
            actor_id=principal.user_id, actor_role=member.role.value, action=action,
            target=f"{service}/{field_id}",
        )
    )
    return {"ok": True, "action": action}
