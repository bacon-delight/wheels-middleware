"""Extract worker (SQS `extract`): extraction -> stored terms -> coverage -> advance."""

from __future__ import annotations

import json
import logging

from ..catalog.resolve import normalise
from ..catalog.store import load_snapshot
from ..config import get_settings
from ..extraction.materialize import build_rows, coverage_rows, merge_rows
from ..extraction.service import extract_contract
from ..messaging import emit_lifecycle_event
from ..objects import extraction_key
from ..store.models import DocumentStanding
from ..store.repository import ConflictError, Repository, utcnow
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


def _process(body: dict) -> None:
    settings = get_settings()
    repo = Repository()
    s3 = S3Store()
    eid, sid = body["engagement_id"], body["submission_id"]
    did, ver = body["document_id"], body["version"]

    pdf = s3.get_bytes(body["s3_key"])
    result = extract_contract(pdf, doc_type_hint=body.get("doc_type"))
    extraction = result.extraction.model_dump(mode="json")
    telemetry = result.telemetry()
    s3.put_json(extraction_key(eid, did, ver), {"extraction": extraction, "run": telemetry})

    v = repo.get_document_version(eid, did, ver)
    if v is not None:
        v.extraction = extraction
        # What the run cost, kept with the document it produced so it can be shown next to it.
        v.extraction_run = telemetry
        v.status = "extracted"
        repo.put_document_version(v)

    catalog = load_snapshot(repo)
    now = utcnow()
    fresh, unmatched = build_rows(
        eid, did, ver, extraction["records"],
        catalog=catalog, threshold=settings.review_confidence_threshold, now=now,
    )
    # Merge rather than overwrite: an analyst's approvals and corrections survive a re-run, and
    # only the terms that actually moved lose theirs.
    existing = repo.list_terms(eid, did, ver)
    write, supersede = merge_rows(existing, fresh)
    if write:
        repo.put_terms(write)
    if supersede:
        repo.put_terms(supersede)

    # Coverage is rebuilt for this document version alone, so removing an agreement withdraws
    # exactly what it contributed and nothing else.
    repo.clear_coverage(eid, did, ver)
    repo.put_coverage(coverage_rows(eid, did, ver, fresh, catalog, now=now))

    # Program names the catalog could not place queue for a person. Never auto-created: a
    # catalog that grows itself stops being a denominator worth measuring against.
    for name in set(unmatched):
        repo.record_unmatched(
            name, normalise(name), {"engagement_id": eid, "document_id": did, "raw": name}
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
        "extracted %s/%s v%s records=%s written=%s superseded=%s "
        "tokens_in=%s tokens_out=%s cached=%s cost=%s unresolved_cites=%s truncated=%s",
        eid, did, ver, len(fresh), len(write), len(supersede),
        result.input_tokens, result.output_tokens, result.cache_read_tokens,
        result.cost_usd, result.unresolved_citations, result.truncated_calls,
    )
