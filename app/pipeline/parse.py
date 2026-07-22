"""Parse worker (SQS `ingest`): render pages + geometry; sanity-check re-uploads.

For a first upload the submission is EXTRACTING, so we render pages and hand off to extract.
For a re-upload the submission is REVALIDATING, so we run the text sanity check first:
pass -> EXTRACTING + continue; fail -> VALIDATION_FAILED and stop.
"""

from __future__ import annotations

import json
import logging

from ..config import get_settings
from ..messaging import emit_lifecycle_event, enqueue_extract
from ..objects import page_key
from ..ocr.pymupdf_engine import PyMuPDFEngine
from ..store.repository import ConflictError, Repository
from ..store.s3 import S3Store
from ..validation.sanity import sanity_check_text

log = logging.getLogger(__name__)


def handler(event, context=None):
    for record in event.get("Records", []):
        _process(json.loads(record["body"]))
    return {"ok": True}


def _process(body: dict) -> None:
    settings = get_settings()
    repo = Repository()
    s3 = S3Store()
    engine = PyMuPDFEngine()

    eid, sid = body["engagement_id"], body["submission_id"]
    did, ver = body["document_id"], body["version"]

    pdf = s3.get_bytes(body["s3_key"])
    parsed = engine.parse(pdf)

    # Re-upload path: sanity-gate before spending an extraction call.
    sub = repo.get_submission(eid, sid)
    if sub is not None and sub.status.value == "REVALIDATING":
        engagement = repo.get_engagement(eid)
        ok, reasons = sanity_check_text(
            parsed.full_text, engagement.client_name if engagement else "", body.get("doc_type", "")
        )
        if not ok:
            try:
                repo.update_submission_status(eid, sid, "REVALIDATING", "VALIDATION_FAILED")
            except ConflictError:
                pass
            emit_lifecycle_event(
                "sanity_fail",
                {"engagement_id": eid, "submission_id": sid, "reasons": "; ".join(reasons)},
            )
            log.warning("sanity failed for %s/%s: %s", eid, did, reasons)
            return
        try:
            repo.update_submission_status(eid, sid, "REVALIDATING", "EXTRACTING")
        except ConflictError:
            pass

    for p in parsed.pages:
        png = engine.render_page_png(pdf, p.page_number, dpi=settings.render_dpi)
        s3.put_bytes(page_key(eid, did, ver, p.page_number), png, "image/png")

    v = repo.get_document_version(eid, did, ver)
    if v is not None:
        v.page_count = parsed.page_count
        v.status = "parsed"
        repo.put_document_version(v)

    enqueue_extract(body)
    log.info("parsed %s/%s v%s pages=%s", eid, did, ver, parsed.page_count)
