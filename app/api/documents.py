"""Documents: presigned upload, page renders (left panel), field review + approve/correct."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError

from ..auth.deps import get_principal, get_repo, get_s3, membership_dep, require_provider
from ..auth.principal import Principal
from ..catalog.resolve import resolve as resolve_service
from ..catalog.store import load_snapshot
from ..extraction.materialize import coverage_rows
from ..extraction.schema import RECORD_MODELS
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
    "CHANGES_REQUESTED_CLIENT", "CHANGES_REQUESTED_AUDIT",
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


@router.delete(
    "/engagements/{engagement_id}/documents/{document_id}/versions/{version}", status_code=200
)
def discard_version(
    engagement_id: str,
    document_id: str,
    version: int,
    member: Membership = Depends(require_provider),
    principal: Principal = Depends(get_principal),
    repo: Repository = Depends(get_repo),
    s3: S3Store = Depends(get_s3),
):
    """Undo an upload whose file never arrived.

    The row is written when the upload is presigned, because the key it points at is built from
    the document and version. If the browser's PUT then fails — a blocked origin, a dropped
    connection — the engagement is left holding an agreement with nothing behind it, which
    reads as a document stuck for ever on "reading…". This is how the caller takes it back.
    """
    doc = repo.get_document(engagement_id, document_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    ver = repo.get_document_version(engagement_id, document_id, version)
    if ver is None:
        raise HTTPException(404, "version not found")
    if ver.status != "uploaded":
        raise HTTPException(409, "this version has already been read; supersede it instead")
    if version == 1:
        # Nothing of this document ever landed, so nothing of it should remain.
        return delete_document(engagement_id, document_id, member, principal, repo, s3)
    repo.discard_document_version(engagement_id, document_id, version)
    s3.delete_prefix(f"{engagement_id}/{document_id}/v{version:04d}")
    return {"document_id": document_id, "current_version": version - 1}


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


class TermPatchIn(BaseModel):
    approved: bool | None = None
    record: dict[str, Any] | None = None
    notes: str | None = None


@router.get("/engagements/{engagement_id}/documents/{document_id}/versions/{version}/terms")
def get_terms(
    engagement_id: str,
    document_id: str,
    version: int,
    category: str | None = None,
    member: Membership = Depends(membership_dep),
    repo: Repository = Depends(get_repo),
):
    """Extracted terms for one document version, optionally one category.

    The counts come back with the rows so the review screen's tab strip renders from one
    request rather than five.
    """
    terms = [t for t in repo.list_terms(engagement_id, document_id, version) if not t.superseded]
    if member.role == Role.CLIENT:
        # Customers see settled terms only, and never the un-triaged backlog: a list of
        # sections Wheels has not finished reading is internal.
        terms = [t for t in terms if t.approved and t.info_type != "uncategorised"]
    counts: dict[str, int] = {}
    for t in terms:
        counts[t.category] = counts.get(t.category, 0) + 1
    shown = [t for t in terms if category is None or t.category == category]
    # Needs-review first, then least confident: the analyst's queue, for free.
    shown.sort(key=lambda t: (not t.needs_review, t.confidence))
    return {
        "terms": shown,
        "counts_by_category": counts,
        "needs_review_count": sum(1 for t in terms if t.needs_review),
        "approved_count": sum(1 for t in terms if t.approved),
        "total": len(terms),
    }


@router.patch(
    "/engagements/{engagement_id}/documents/{document_id}/versions/{version}"
    "/terms/{category}/{record_id}"
)
def patch_term(
    engagement_id: str,
    document_id: str,
    version: int,
    category: str,
    record_id: str,
    body: TermPatchIn,
    member: Membership = Depends(require_provider),
    principal: Principal = Depends(get_principal),
    repo: Repository = Depends(get_repo),
):
    """Approve or correct one term.

    A correction is re-validated against the model for that record's own type, so a definition
    cannot acquire an amount and a priced line cannot lose its shape. Editing the program on a
    pricing term re-runs catalog resolution and rewrites this engagement's coverage, because
    that correction is the one that changes what the customer is shown to be buying.
    """
    term = repo.get_term(engagement_id, document_id, version, category, record_id)
    if term is None:
        raise HTTPException(404, "term not found")

    updates: dict[str, Any] = {}
    action = "term_reviewed"

    if body.record is not None:
        merged = {**term.record, **body.record}
        model = RECORD_MODELS.get(term.info_type)
        if model is None:
            raise HTTPException(400, f"unknown record type {term.info_type}")
        try:
            validated = model.model_validate(merged).model_dump(mode="json")
        except ValidationError as e:
            raise HTTPException(400, f"that correction does not fit a {term.info_type}: {e}") from e
        updates["record"] = validated
        updates["corrected"] = True
        updates["title"] = validated.get("item") or validated.get("term") or term.title
        updates["amount"] = validated.get("amount")
        updates["frequency"] = validated.get("frequency")
        action = "term_corrected"

        if term.info_type == "pricing_item":
            snapshot = load_snapshot(repo)
            found = resolve_service(snapshot, validated.get("program"), validated.get("item"))
            updates["program_id"] = found.program_id
            updates["catalog_item_id"] = found.item_id
            updates["catalog_match"] = found.kind
            term.record = validated
            term.program_id = found.program_id
            term.catalog_item_id = found.item_id

    if body.notes is not None:
        updates["notes"] = body.notes
    if body.approved:
        updates["approved"] = True
        updates["needs_review"] = False
        updates["changed_since_approval"] = False
        if action == "term_reviewed":
            action = "term_approved"
    if not updates:
        raise HTTPException(400, "no changes provided")

    updates["updated_at"] = utcnow()
    repo.update_term(engagement_id, document_id, version, category, record_id, updates)

    if term.info_type == "pricing_item" and body.record is not None:
        _rebuild_coverage(repo, engagement_id, document_id, version)

    repo.put_audit(
        AuditEvent(
            engagement_id=engagement_id, event_id=new_id(), ts=utcnow(),
            actor_id=principal.user_id, actor_role=member.role.value,
            actor_name=member.name or principal.name, action=action,
            target=f"{category}/{term.title[:60]}",
        )
    )
    return {"ok": True, "action": action}


@router.post(
    "/engagements/{engagement_id}/documents/{document_id}/versions/{version}/terms:approve"
)
def approve_terms(
    engagement_id: str,
    document_id: str,
    version: int,
    category: str | None = None,
    member: Membership = Depends(require_provider),
    principal: Principal = Depends(get_principal),
    repo: Repository = Depends(get_repo),
):
    """Approve a whole category at once.

    The one-request-per-term loop this replaces was fine at thirteen terms and is three hundred
    round trips at the volume a real contract produces.
    """
    terms = [
        t
        for t in repo.list_terms(engagement_id, document_id, version, category)
        if not t.superseded and not t.approved
    ]
    now = utcnow()
    for t in terms:
        repo.update_term(
            engagement_id, document_id, version, t.category, t.record_id,
            {"approved": True, "needs_review": False, "changed_since_approval": False,
             "updated_at": now},
        )
    repo.put_audit(
        AuditEvent(
            engagement_id=engagement_id, event_id=new_id(), ts=utcnow(),
            actor_id=principal.user_id, actor_role=member.role.value,
            actor_name=member.name or principal.name, action="terms_approved",
            target=f"{category or 'all'} ({len(terms)})",
        )
    )
    return {"ok": True, "approved": len(terms)}


def _rebuild_coverage(repo: Repository, eid: str, did: str, ver: int) -> None:
    snapshot = load_snapshot(repo)
    rows = [t for t in repo.list_terms(eid, did, ver) if not t.superseded]
    repo.clear_coverage(eid, did, ver)
    repo.put_coverage(coverage_rows(eid, did, ver, rows, snapshot, now=utcnow()))


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
