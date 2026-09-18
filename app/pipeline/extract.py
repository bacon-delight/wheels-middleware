"""Extract worker (SQS `extract`): LLM extraction -> review fields -> advance the submission."""

from __future__ import annotations

import json
import logging

from ..config import get_settings
from ..extraction.service import extract_contract
from ..messaging import emit_lifecycle_event
from ..objects import extraction_key
from ..store.models import DocumentStanding, ReviewField
from ..store.repository import ConflictError, Repository, new_id
from ..store.s3 import S3Store

log = logging.getLogger(__name__)


def _all_documents_extracted(repo, eid: str) -> bool:
    """True once every document in force has a fully extracted current version.

    This counts the engagement's documents, not the submission's typed slots. A document that
    parsing could not identify as a lease or service agreement never enters a slot, so keying
    off slots left such a submission stuck in EXTRACTING forever — the pipeline finished but
    nothing was there to satisfy the guard.
    """
    documents = [
        d for d in repo.list_documents(eid)
        if d.standing != DocumentStanding.SUPERSEDED.value
    ]
    if not documents:
        return False
    for doc in documents:
        version = repo.get_document_version(eid, doc.document_id, doc.current_version)
        if version is None or version.status != "extracted":
            return False
    return True

def handler(event, context=None):
    for record in event.get("Records", []):
        _process(json.loads(record["body"]))
    return {"ok": True}


def _materialize_field(repo, eid, did, ver, service, elected, confidence, fee_items, citations,
                       threshold, notes=None):
    needs = bool(elected) and float(confidence) <= threshold
    repo.put_field(
        ReviewField(
            engagement_id=eid, document_id=did, version=ver, field_id=new_id(),
            service=service, elected=bool(elected), confidence=float(confidence),
            needs_review=needs, fee_items=fee_items or [], citations=citations or [], notes=notes,
        )
    )


def _process(body: dict) -> None:
    settings = get_settings()
    repo = Repository()
    s3 = S3Store()
    eid, sid = body["engagement_id"], body["submission_id"]
    did, ver = body["document_id"], body["version"]

    pdf = s3.get_bytes(body["s3_key"])
    result = extract_contract(pdf, doc_type_hint=body.get("doc_type"))
    extraction = result.extraction.model_dump(mode="json")
    s3.put_json(extraction_key(eid, did, ver), extraction)

    v = repo.get_document_version(eid, did, ver)
    if v is not None:
        v.extraction = extraction
        v.status = "extracted"
        repo.put_document_version(v)

    threshold = settings.review_confidence_threshold
    for sl in extraction["service_lines"]:
        _materialize_field(
            repo, eid, did, ver, sl["service"], sl["elected"], sl["confidence"],
            sl["fee_items"], sl["citations"], threshold,
        )
    for bf in extraction.get("bundled_fees", []):
        _materialize_field(
            repo, eid, did, ver, "BundledManagementFee", True, bf.get("confidence", 0.0),
            bf["fee_items"], bf.get("citations", []), threshold,
            notes="Covers: " + ", ".join(bf.get("covers_services", [])),
        )

    # Advance only once every document in force has been extracted. "First one wins" would push
    # a two-agreement engagement into underwriting with half its terms missing.
    if _all_documents_extracted(repo, eid):
        try:
            repo.update_submission_status(eid, sid, "EXTRACTING", "IN_UNDERWRITING")
            emit_lifecycle_event(
                "pipeline_done",
                {"engagement_id": eid, "submission_id": sid, "status": "IN_UNDERWRITING"},
            )
        except ConflictError:
            pass
    log.info(
        "extracted %s/%s v%s tokens_in=%s tokens_out=%s unresolved_cites=%s",
        eid, did, ver, result.input_tokens, result.output_tokens, result.unresolved_citations,
    )
