"""Generate the billing configuration from a submission's approved terms.

This is the "no manual keying" hand-off: the elected service lines (and MLA lease terms) are
aggregated into a machine-readable config and written to S3. A real billing-system integration
would consume this artifact.
"""

from __future__ import annotations

from typing import Any

from ..objects import billing_config_key
from ..store.repository import Repository, utcnow


def build_billing_config(
    repo: Repository, engagement_id: str, submission_id: str, s3=None
) -> dict[str, Any]:
    sub = repo.get_submission(engagement_id, submission_id)
    engagement = repo.get_engagement(engagement_id)
    config: dict[str, Any] = {
        "engagement_id": engagement_id,
        "submission_id": submission_id,
        "client_name": engagement.client_name if engagement else None,
        "generated_at": utcnow(),
        "service_lines": [],
        "lease_terms": None,
        "excluded_services": [],
    }
    if sub is None:
        return config

    for document_id in (sub.msa_document_id, sub.mla_document_id):
        if not document_id:
            continue
        doc = repo.get_document(engagement_id, document_id)
        if doc is None:
            continue
        version = repo.get_document_version(engagement_id, document_id, doc.current_version)
        fields = repo.list_fields(engagement_id, document_id, doc.current_version)
        for f in fields:
            if f.elected:
                config["service_lines"].append(
                    {"service": f.service, "fee_items": f.fee_items, "approved": f.approved}
                )
            else:
                # Explicitly record non-elected services so they are never billed.
                config["excluded_services"].append(f.service)
        if version and version.extraction and version.extraction.get("lease_terms"):
            config["lease_terms"] = version.extraction["lease_terms"]

    if s3 is None:
        from ..store.s3 import S3Store

        s3 = S3Store()
    s3.put_json(billing_config_key(engagement_id, submission_id), config)
    return config
