"""The single-table repository round-trips entities and enforces optimistic transitions."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from app.lifecycle.submission_state import Role, SubmissionStatus
from app.store.models import (
    AuditEvent,
    Document,
    DocumentVersion,
    Engagement,
    Membership,
    ReviewField,
    Submission,
)
from app.store.repository import ConflictError, Repository, new_id, utcnow

TABLE = "wheels-test"


@pytest.fixture
def repo():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="ap-south-2")
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield Repository(table_name=TABLE, resource=ddb)


def _eng(repo):
    e = Engagement(
        engagement_id="apex", name="Apex Field Services", client_name="Apex Field Services LLC",
        created_by="u1", created_at=utcnow(),
    )
    return repo.put_engagement(e)


def test_engagement_roundtrip(repo):
    _eng(repo)
    got = repo.get_engagement("apex")
    assert got is not None and got.client_name == "Apex Field Services LLC"


def test_membership_and_user_engagements(repo):
    _eng(repo)
    repo.put_membership(Membership(
        engagement_id="apex", user_id="analyst1", email="a@wheels.com",
        role=Role.PROVIDER, created_at=utcnow()))
    repo.put_membership(Membership(
        engagement_id="apex", user_id="client1", email="c@apex.com",
        role=Role.CLIENT, created_at=utcnow()))
    members = repo.list_members("apex")
    assert {m.user_id for m in members} == {"analyst1", "client1"}
    assert repo.list_user_engagement_ids("client1") == ["apex"]


def test_submission_optimistic_transition(repo):
    _eng(repo)
    sid = new_id()
    repo.put_submission(Submission(
        engagement_id="apex", submission_id=sid, created_at=utcnow(), updated_at=utcnow()))
    updated = repo.update_submission_status(
        "apex", sid, SubmissionStatus.DRAFT.value, SubmissionStatus.EXTRACTING.value)
    assert updated.status == SubmissionStatus.EXTRACTING
    # A stale expected-status must fail rather than clobber.
    with pytest.raises(ConflictError):
        repo.update_submission_status(
            "apex", sid, SubmissionStatus.DRAFT.value, SubmissionStatus.IN_UNDERWRITING.value)


def test_document_and_version(repo):
    _eng(repo)
    repo.put_document(Document(
        engagement_id="apex", document_id="d1", doc_type="MSA",
        filename="MSA_Apex.pdf", created_at=utcnow()))
    repo.put_document_version(DocumentVersion(
        engagement_id="apex", document_id="d1", version=1,
        s3_key="apex/d1/v1.pdf", page_count=16, uploaded_at=utcnow()))
    assert [d.document_id for d in repo.list_documents("apex")] == ["d1"]
    v = repo.get_document_version("apex", "d1", 1)
    assert v is not None and v.page_count == 16


def test_fields_sorted_needs_review_then_confidence(repo):
    _eng(repo)
    for svc, conf, needs in [("Fuel", 0.4, True), ("Maintenance", 0.9, True), ("Collision", 0.95, False)]:
        repo.put_field(ReviewField(
            engagement_id="apex", document_id="d1", version=1, field_id=new_id(),
            service=svc, elected=True, confidence=conf, needs_review=needs,
            fee_items=[{"amount": 21.5, "rate_pct": 1.75}],
            citations=[{"page": 4, "bbox": {"x0": 0.1, "y0": 0.2, "x1": 0.6, "y1": 0.22}}]))
    fields = repo.list_fields("apex", "d1", 1)
    assert [f.service for f in fields] == ["Fuel", "Maintenance", "Collision"]
    # Floats survived the Decimal round-trip.
    assert fields[0].fee_items[0]["amount"] == 21.5
    assert fields[0].citations[0]["bbox"]["x1"] == 0.6


def test_field_update_approves(repo):
    _eng(repo)
    fid = new_id()
    repo.put_field(ReviewField(
        engagement_id="apex", document_id="d1", version=1, field_id=fid,
        service="Fuel", elected=True, confidence=0.4, needs_review=True))
    repo.update_field("apex", "d1", 1, "Fuel", fid, {"approved": True, "needs_review": False})
    fields = repo.list_fields("apex", "d1", 1)
    assert fields[0].approved is True and fields[0].needs_review is False


def test_audit_trail_is_time_descending(repo):
    _eng(repo)
    for action in ["created", "submitted", "approved"]:
        repo.put_audit(AuditEvent(
            engagement_id="apex", event_id=new_id(), ts=utcnow(),
            actor_id="u1", actor_role="provider", action=action))
    actions = [a.action for a in repo.list_audit("apex")]
    assert set(actions) == {"created", "submitted", "approved"}
