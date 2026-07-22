"""End-to-end API test: engagement -> lifecycle transitions -> field review -> billing.

Exercises the HTTP surface with moto-backed DynamoDB + S3 and overridden auth. The LLM
pipeline is not run here; we seed the post-extraction state directly to drive the lifecycle.
"""

from __future__ import annotations

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app.auth.deps import get_principal, get_repo, get_s3
from app.auth.principal import Principal
from app.lifecycle.submission_state import Role, SubmissionStatus
from app.main import app
from app.store.models import Membership, ReviewField
from app.store.repository import Repository, utcnow
from app.store.s3 import S3Store

REGION = "ap-south-2"


def _make_table(ddb):
    ddb.create_table(
        TableName="wheels-test",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "GSI1",
            "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"}, {"AttributeName": "GSI1SK", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def ctx():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        _make_table(ddb)
        s3c = boto3.client("s3", region_name=REGION)
        s3c.create_bucket(Bucket="wheels-test-docs", CreateBucketConfiguration={"LocationConstraint": REGION})
        repo = Repository(table_name="wheels-test", resource=ddb)
        s3 = S3Store(bucket="wheels-test-docs", client=s3c)

        state = {"principal": Principal(user_id="analyst1", email="a@wheels.com", groups=["provider"])}
        app.dependency_overrides[get_repo] = lambda: repo
        app.dependency_overrides[get_s3] = lambda: s3
        app.dependency_overrides[get_principal] = lambda: state["principal"]

        yield TestClient(app), repo, state
        app.dependency_overrides.clear()


def _as(state, principal):
    state["principal"] = principal


def test_full_lifecycle_and_tenancy(ctx):
    client, repo, state = ctx

    # Provider creates an engagement (+ its first submission).
    r = client.post("/engagements", json={"name": "Apex", "client_name": "Apex Field Services LLC"})
    assert r.status_code == 201, r.text
    eid = r.json()["engagement"]["engagement_id"]
    sid = r.json()["submission_id"]

    # It shows up in the provider's list and detail.
    assert eid in [e["engagement_id"] for e in client.get("/engagements").json()["engagements"]]
    assert client.get(f"/engagements/{eid}").json()["your_role"] == "provider"

    # Presign an MSA upload.
    r = client.post(f"/engagements/{eid}/documents:presign",
                    json={"doc_type": "MSA", "filename": "MSA_Apex.pdf", "submission_id": sid})
    assert r.status_code == 201
    did = r.json()["document_id"]
    assert "upload_url" in r.json()

    # Seed post-extraction state: submission in underwriting + review fields.
    repo.update_submission_status(eid, sid, SubmissionStatus.DRAFT.value, SubmissionStatus.EXTRACTING.value)
    repo.update_submission_status(eid, sid, SubmissionStatus.EXTRACTING.value, SubmissionStatus.IN_UNDERWRITING.value)
    for svc, elected, conf in [("Rentals", False, 0.99), ("Fuel", True, 0.6), ("Collision", True, 0.95)]:
        repo.put_field(ReviewField(
            engagement_id=eid, document_id=did, version=1, field_id=svc.lower(),
            service=svc, elected=elected, confidence=conf, needs_review=(elected and conf <= 0.8),
            fee_items=[{"rate_pct": 1.75}], citations=[{"page": 3, "bbox": {"x0": 0.1, "y0": 0.2, "x1": 0.5, "y1": 0.22}}]))

    # Review queue: needs-review first; Rentals present but not elected.
    fields = client.get(f"/engagements/{eid}/documents/{did}/versions/1/fields").json()
    assert fields["needs_review_count"] == 1
    assert fields["fields"][0]["service"] == "Fuel"  # lowest confidence, needs review, sorted first
    assert any(f["service"] == "Rentals" and f["elected"] is False for f in fields["fields"])

    # Approve the flagged field.
    r = client.patch(f"/engagements/{eid}/documents/{did}/versions/1/fields/Fuel/fuel",
                     json={"approved": True})
    assert r.status_code == 200 and r.json()["action"] == "field_approved"

    # Provider submits to client.
    r = client.post(f"/engagements/{eid}/submissions/{sid}:submit-to-client")
    assert r.json()["submission"]["status"] == "PENDING_CLIENT_APPROVAL"

    # A non-member cannot see the engagement (tenant isolation).
    _as(state, Principal(user_id="stranger", email="s@x.com", groups=["provider"]))
    assert client.get(f"/engagements/{eid}").status_code == 403

    # The client user approves -> fields captured -> finance review.
    repo.put_membership(Membership(engagement_id=eid, user_id="client1", email="c@apex.com", role=Role.CLIENT, created_at=utcnow()))
    _as(state, Principal(user_id="client1", email="c@apex.com", groups=["client"]))
    # Client may only approve/request-changes, and only sees approved fields.
    assert client.get(f"/engagements/{eid}/documents/{did}/versions/1/fields").json()["needs_review_count"] == 0
    r = client.post(f"/engagements/{eid}/submissions/{sid}:client-approve")
    assert r.json()["submission"]["status"] == "PENDING_FINANCE_APPROVAL"

    # Finance (provider role) approves and sets up billing -> ACTIVE.
    _as(state, Principal(user_id="analyst1", email="a@wheels.com", groups=["provider"]))
    assert client.post(f"/engagements/{eid}/submissions/{sid}:finance-approve").json()["submission"]["status"] == "FINANCE_APPROVED"
    r = client.post(f"/engagements/{eid}/submissions/{sid}:setup-billing")
    assert r.json()["submission"]["status"] == "ACTIVE"

    # Billing config was written with Rentals excluded.
    import json
    cfg = json.loads(S3Store(bucket="wheels-test-docs", client=boto3.client("s3", region_name=REGION)).get_bytes(f"{eid}/{sid}/billing-config.json"))
    assert "Rentals" in cfg["excluded_services"]


def test_client_cannot_create_engagement(ctx):
    client, repo, state = ctx
    _as(state, Principal(user_id="client1", email="c@apex.com", groups=["client"]))
    assert client.post("/engagements", json={"name": "X", "client_name": "Y"}).status_code == 403


def test_illegal_transition_returns_409(ctx):
    client, repo, state = ctx
    r = client.post("/engagements", json={"name": "Apex", "client_name": "Apex LLC"})
    eid, sid = r.json()["engagement"]["engagement_id"], r.json()["submission_id"]
    # Can't submit-to-client straight from DRAFT.
    assert client.post(f"/engagements/{eid}/submissions/{sid}:submit-to-client").status_code == 409
