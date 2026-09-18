"""Keep an engagement's agreements, standing and scope consistent after any upload.

One place does this so the parse worker, a manual type correction and the seed script cannot
drift apart. Three things are settled together because they depend on each other: which
agreement of each type is in force, what the engagement therefore covers, and which documents
the current submission points at for extraction and billing.
"""

from __future__ import annotations

import logging

from ..store.models import DocumentStanding
from ..store.repository import Repository
from .classify import derive_scope, rank_standing

log = logging.getLogger(__name__)


def pending_documents(repo: Repository, engagement_id: str) -> list:
    """Documents in force whose current version has not been through extraction.

    A document is pending when it has just been uploaded, or when a replacement bumped it to a
    version nothing has read yet. Extraction targets exactly these: re-reading an agreement
    that has already been extracted costs a model call and throws away the analyst's approvals
    on terms that did not change.
    """
    out = []
    for d in repo.list_documents(engagement_id):
        if d.standing == DocumentStanding.SUPERSEDED.value:
            continue
        version = repo.get_document_version(engagement_id, d.document_id, d.current_version)
        if version is None or version.status != "extracted":
            out.append(d)
    return out


def reconcile_engagement(repo: Repository, engagement_id: str) -> dict:
    """Recompute standing, scope and the submission's document slots. Idempotent."""
    documents = repo.list_documents(engagement_id)
    if not documents:
        return {"scope": None, "current": [], "superseded": []}

    ranked = rank_standing([d.model_dump(mode="json") for d in documents])
    for d in documents:
        want = ranked.get(d.document_id, DocumentStanding.CURRENT.value)
        if d.standing != want:
            repo.set_document_meta(engagement_id, d.document_id, standing=want)
            d.standing = want

    current = [d for d in documents if d.standing == DocumentStanding.CURRENT.value]
    scope = derive_scope({d.doc_type for d in current if d.doc_type != "UNKNOWN"})
    engagement = repo.get_engagement(engagement_id)
    if engagement is not None and engagement.scope != scope:
        repo.set_engagement_scope(engagement_id, scope)

    # Only the agreements in force are under negotiation; superseded ones are record-keeping and
    # must not feed extraction, terms or billing.
    subs = repo.list_submissions(engagement_id)
    if subs:
        sub = subs[0]
        slots = {d.doc_type: d.document_id for d in current if d.doc_type != "UNKNOWN"}
        if slots != sub.docs():
            sub.document_ids = slots
            sub.msa_document_id = slots.get("MSA")
            sub.mla_document_id = slots.get("MLA")
            repo.put_submission(sub)

    return {
        "scope": scope,
        "current": [d.document_id for d in current],
        "superseded": [d.document_id for d in documents if d.standing != "CURRENT"],
    }
