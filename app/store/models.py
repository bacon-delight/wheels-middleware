"""Stored domain models (distinct from the extraction schema).

These are the shapes persisted to DynamoDB / returned by the API. Money values inside
`fee_items` / `citations` are stored as-is (converted to Decimal at the DynamoDB boundary by
the repository).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..lifecycle.submission_state import Role, SubmissionStatus


class Engagement(BaseModel):
    engagement_id: str
    name: str
    client_name: str
    status: str = "onboarding"  # engagement-level lifecycle label
    created_by: str
    created_at: str


class Membership(BaseModel):
    engagement_id: str
    user_id: str
    email: str
    role: Role
    name: str | None = None
    phone: str | None = None
    created_at: str


class UserProfile(BaseModel):
    user_id: str
    email: str
    name: str | None = None
    phone: str | None = None
    onboarded: bool = False
    updated_at: str | None = None


class Submission(BaseModel):
    engagement_id: str
    submission_id: str
    status: SubmissionStatus = SubmissionStatus.DRAFT
    round: int = 1
    msa_document_id: str | None = None
    mla_document_id: str | None = None
    latest_comment: str | None = None
    latest_comment_by: str | None = None  # display name of who left latest_comment
    client_signature: dict[str, Any] | None = None  # {full_name, place, signed_at}
    created_at: str
    updated_at: str


class Document(BaseModel):
    engagement_id: str
    document_id: str
    doc_type: str  # MSA | MLA
    filename: str
    current_version: int = 1
    created_at: str


class DocumentVersion(BaseModel):
    engagement_id: str
    document_id: str
    version: int
    s3_key: str
    page_count: int | None = None
    status: str = "uploaded"  # uploaded | parsed | extracted | failed
    uploaded_at: str
    # Full ContractExtraction JSON (small); the review queue is materialized as ReviewFields.
    extraction: dict[str, Any] | None = None


class ReviewField(BaseModel):
    engagement_id: str
    document_id: str
    version: int
    field_id: str
    service: str
    elected: bool
    confidence: float
    needs_review: bool
    approved: bool = False
    corrected: bool = False
    fee_items: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    notes: str | None = None


class AuditEvent(BaseModel):
    engagement_id: str
    event_id: str
    ts: str
    actor_id: str
    actor_role: str
    actor_name: str | None = None
    action: str
    target: str | None = None
    comment: str | None = None
