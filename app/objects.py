"""S3 object key builders (shared by the API and the pipeline)."""

from __future__ import annotations


def source_pdf_key(engagement_id: str, document_id: str, version: int) -> str:
    return f"{engagement_id}/{document_id}/v{version:04d}.pdf"


def page_key(engagement_id: str, document_id: str, version: int, page: int) -> str:
    return f"{engagement_id}/{document_id}/v{version:04d}/pages/{page:04d}.png"


def extraction_key(engagement_id: str, document_id: str, version: int) -> str:
    return f"{engagement_id}/{document_id}/v{version:04d}/extraction.json"


def billing_config_key(engagement_id: str, submission_id: str) -> str:
    return f"{engagement_id}/{submission_id}/billing-config.json"
