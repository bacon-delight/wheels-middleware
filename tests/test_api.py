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
from app.store.models import Membership
from app.store.repository import Repository, utcnow
from app.store.s3 import S3Store

from .pricing_helpers import seed_pricing

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
            {"AttributeName": "GSI2PK", "AttributeType": "S"},
            {"AttributeName": "GSI2SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "GSI1",
                "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"}, {"AttributeName": "GSI1SK", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "GSI2",
                "KeySchema": [{"AttributeName": "GSI2PK", "KeyType": "HASH"}, {"AttributeName": "GSI2SK", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _make_vehicles_table(ddb):
    ddb.create_table(
        TableName="wheels-test-vehicles",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
            {"AttributeName": "GSI2PK", "AttributeType": "S"},
            {"AttributeName": "GSI2SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "GSI1",
                "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"}, {"AttributeName": "GSI1SK", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "GSI2",
                "KeySchema": [{"AttributeName": "GSI2PK", "KeyType": "HASH"}, {"AttributeName": "GSI2SK", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def ctx():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        _make_table(ddb)
        _make_vehicles_table(ddb)
        s3c = boto3.client("s3", region_name=REGION)
        s3c.create_bucket(Bucket="wheels-test-docs", CreateBucketConfiguration={"LocationConstraint": REGION})
        repo = Repository(
            table_name="wheels-test", resource=ddb, vehicles_table_name="wheels-test-vehicles"
        )
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
    for program, item, conf in [
        ("Rental Program", "Rental Administration Fee", 0.99),
        ("Fuel Management Program", "Fuel Card Fee", 0.6),
        ("Collision Management Program", "Claim Loss Notice Fee", 0.95),
    ]:
        seed_pricing(repo, eid, did, 1, program=program, item=item, amount=1.75,
                     frequency="per transaction", approved=False, confidence=conf)

    # Review queue: needs-review first, so the least confident term leads.
    terms = client.get(f"/engagements/{eid}/documents/{did}/versions/1/terms").json()
    assert terms["total"] == 3
    assert terms["counts_by_category"]["pricing"] == 3
    assert terms["terms"][0]["record"]["item"] == "Fuel Card Fee"  # lowest confidence

    # Approve the flagged term.
    flagged = terms["terms"][0]
    r = client.patch(
        f"/engagements/{eid}/documents/{did}/versions/1/terms/pricing/{flagged['record_id']}",
        json={"approved": True},
    )
    assert r.status_code == 200 and r.json()["action"] == "term_approved"

    # Provider submits to client.
    r = client.post(f"/engagements/{eid}/submissions/{sid}:submit-to-client")
    assert r.json()["submission"]["status"] == "PENDING_CLIENT_APPROVAL"

    # Org-level access: a non-member provider (Wheels staff) can open any engagement...
    _as(state, Principal(user_id="stranger", email="s@x.com", groups=["provider"]))
    assert client.get(f"/engagements/{eid}").status_code == 200
    # ...but a client who isn't a member of this engagement stays isolated (tenant boundary).
    _as(state, Principal(user_id="outsider", email="o@other.com", groups=["client"]))
    assert client.get(f"/engagements/{eid}").status_code == 403

    # The client user approves -> fields captured -> finance review.
    repo.put_membership(Membership(engagement_id=eid, user_id="client1", email="c@apex.com", role=Role.CLIENT, created_at=utcnow()))
    _as(state, Principal(user_id="client1", email="c@apex.com", groups=["client"]))
    # Client may only approve/request-changes, and only sees approved fields.
    assert client.get(f"/engagements/{eid}/documents/{did}/versions/1/fields").json()["needs_review_count"] == 0
    r = client.post(f"/engagements/{eid}/submissions/{sid}:client-approve",
                    json={"signature": {"full_name": "Jordan Lee", "place": "Austin, TX"}})
    # Signing builds the billing on the spot, so what comes back is something to audit rather
    # than another queue to wait in.
    assert r.json()["submission"]["status"] == "PENDING_BILLING_AUDIT"

    # The audit reads the generated billing and takes the engagement live.
    _as(state, Principal(user_id="analyst1", email="a@wheels.com", groups=["provider"]))
    r = client.post(f"/engagements/{eid}/submissions/{sid}:approve-billing")
    assert r.json()["submission"]["status"] == "ACTIVE"

    # The billing config carries the priced terms, grouped by the program they belong to.
    import json
    cfg = json.loads(S3Store(bucket="wheels-test-docs", client=boto3.client("s3", region_name=REGION)).get_bytes(f"{eid}/{sid}/billing-config.json"))
    assert {line["service"] for line in cfg["service_lines"]} == {
        "Rental Program", "Fuel Management Program", "Collision Management Program",
    }
    # Every priced line records what kind of charge it is, so the ones a per-vehicle estimate
    # cannot use are visible rather than silently dropped.
    assert all(item["billing_class"] for item in cfg["pricing_items"])


def test_client_cannot_create_engagement(ctx):
    client, repo, state = ctx
    _as(state, Principal(user_id="client1", email="c@apex.com", groups=["client"]))
    assert client.post("/engagements", json={"name": "X", "client_name": "Y"}).status_code == 403


def _drive_to_active(client, repo, state, sid_holder):
    """Take a fresh engagement to ACTIVE with one $4/vehicle/month recurring fee."""
    r = client.post("/engagements", json={"name": "Apex", "client_name": "Apex LLC"})
    eid, sid = r.json()["engagement"]["engagement_id"], r.json()["submission_id"]
    did = client.post(f"/engagements/{eid}/documents:presign",
                      json={"doc_type": "MSA", "filename": "m.pdf", "submission_id": sid}).json()["document_id"]
    repo.update_submission_status(eid, sid, SubmissionStatus.DRAFT.value, SubmissionStatus.EXTRACTING.value)
    repo.update_submission_status(eid, sid, SubmissionStatus.EXTRACTING.value, SubmissionStatus.IN_UNDERWRITING.value)
    seed_pricing(repo, eid, did, 1, program="Fuel Management Program",
                  item="Fuel Fee", amount=4.0)
    # No vehicles are assigned here, so the derived fleet is 0; the provider sets the billed
    # size explicitly during the approval stages, as they would in the real flow.
    client.patch(f"/engagements/{eid}/billing", json={"fleet_size": 100})
    client.post(f"/engagements/{eid}/submissions/{sid}:submit-to-client")
    repo.put_membership(Membership(engagement_id=eid, user_id="client1", email="c@apex.com", role=Role.CLIENT, created_at=utcnow()))
    _as(state, Principal(user_id="client1", email="c@apex.com", groups=["client"]))
    client.post(f"/engagements/{eid}/submissions/{sid}:client-approve",
                json={"signature": {"full_name": "Jordan Lee", "place": "Austin"}})
    # Signing generates the billing itself; the only thing left is auditing what it produced.
    _as(state, Principal(user_id="analyst1", email="a@wheels.com", groups=["provider"]))
    client.post(f"/engagements/{eid}/submissions/{sid}:approve-billing")
    return eid


def test_finance_dashboard_totals(ctx):
    client, repo, state = ctx
    eid = _drive_to_active(client, repo, state, None)

    d = client.get("/finance/dashboard").json()
    assert d["totals"]["engagements"] == 1
    assert d["totals"]["active"] == 1
    assert d["totals"]["monthly_recurring"] == 400.0  # $4 * 100 vehicles
    assert d["totals"]["annualized"] == 4800.0
    assert d["totals"]["total_fleet"] == 100
    # The funnel is grouped by the five lifecycle steps the whole product names.
    assert next(s["count"] for s in d["funnel"] if s["key"] == "ACTIVE") == 1
    row = next(r for r in d["engagements"] if r["engagement_id"] == eid)
    assert row["monthly_recurring"] == 400.0 and row["status"] == "ACTIVE"

    # Fleet size is locked once billing is active (finalized during the approval stages).
    assert client.patch(f"/engagements/{eid}/billing", json={"fleet_size": 250}).status_code == 409

    # Clients may not see the finance dashboard.
    _as(state, Principal(user_id="client1", email="c@apex.com", groups=["client"]))
    assert client.get("/finance/dashboard").status_code == 403


def test_fleet_size_locked_once_billing_active(ctx):
    client, repo, state = ctx
    eid = _drive_to_active(client, repo, state, None)
    # Fleet can no longer be changed once billing is active.
    assert client.patch(f"/engagements/{eid}/billing", json={"fleet_size": 250}).status_code == 409


def test_fleet_set_during_approval_drives_dues(ctx):
    client, repo, state = ctx
    r = client.post("/engagements", json={"name": "Apex", "client_name": "Apex LLC"})
    eid, sid = r.json()["engagement"]["engagement_id"], r.json()["submission_id"]
    did = client.post(f"/engagements/{eid}/documents:presign",
                      json={"doc_type": "MSA", "filename": "m.pdf", "submission_id": sid}).json()["document_id"]
    repo.update_submission_status(eid, sid, SubmissionStatus.DRAFT.value, SubmissionStatus.EXTRACTING.value)
    repo.update_submission_status(eid, sid, SubmissionStatus.EXTRACTING.value, SubmissionStatus.IN_UNDERWRITING.value)
    seed_pricing(repo, eid, did, 1, program="Fuel Management Program",
                  item="Fuel Fee", amount=4.0)
    # Fleet is finalized during the approval stage.
    assert client.patch(f"/engagements/{eid}/billing", json={"fleet_size": 250}).status_code == 200
    client.post(f"/engagements/{eid}/submissions/{sid}:submit-to-client")
    repo.put_membership(Membership(engagement_id=eid, user_id="client1", email="c@apex.com", role=Role.CLIENT, created_at=utcnow()))
    _as(state, Principal(user_id="client1", email="c@apex.com", groups=["client"]))
    client.post(f"/engagements/{eid}/submissions/{sid}:client-approve",
                json={"signature": {"full_name": "Jordan Lee", "place": "Austin"}})
    _as(state, Principal(user_id="analyst1", email="a@wheels.com", groups=["provider"]))
    client.post(f"/engagements/{eid}/submissions/{sid}:finance-approve")
    client.post(f"/engagements/{eid}/submissions/{sid}:setup-billing")
    b = client.get(f"/engagements/{eid}/billing").json()
    assert b["fleet_size"] == 250 and b["monthly_recurring"] == 1000.0  # $4 x 250


def test_change_review_and_summary(ctx):
    client, repo, state = ctx
    r = client.post("/engagements", json={"name": "Apex", "client_name": "Apex LLC"})
    eid, sid = r.json()["engagement"]["engagement_id"], r.json()["submission_id"]
    did = client.post(f"/engagements/{eid}/documents:presign",
                      json={"doc_type": "MSA", "filename": "m.pdf", "submission_id": sid}).json()["document_id"]
    seed_pricing(repo, eid, did, 1, program="Maintenance Assistance Program",
                 item="Monthly Program Fee", amount=12.5)
    from app.store.models import AuditEvent
    repo.put_audit(AuditEvent(
        engagement_id=eid, event_id="a1", ts=utcnow(), actor_id="c", actor_role="client",
        actor_name="Jordan Lee", action="client_request_changes", comment="reduce maintenance to $10"))
    # Provider re-uploads a revised version with a partial reduction. Naming the document is
    # what distinguishes a revision of this agreement from a second, superseding one.
    client.post(f"/engagements/{eid}/documents:presign",
                json={"document_id": did, "filename": "m2.pdf", "submission_id": sid})
    seed_pricing(repo, eid, did, 2, program="Maintenance Assistance Program",
                 item="Monthly Program Fee", amount=11.0)

    cr = client.get(f"/engagements/{eid}/submissions/{sid}/change-review").json()
    assert cr["applicable"] is True
    assert any("Maintenance" in c["service"] for c in cr["changes"])
    assert cr["items"]  # LLM or deterministic fallback

    s = client.get(f"/engagements/{eid}/summary").json()
    assert any(t["action"] == "client_request_changes" for t in s["thread"])
    assert s["summary"]


def test_payment_schedule_pay_and_remind(ctx):
    client, repo, state = ctx
    eid = _drive_to_active(client, repo, state, None)  # ends as provider (analyst1)

    b = client.get(f"/engagements/{eid}/billing").json()
    sched = b["schedule"]
    assert len(sched) == 12  # monthly cadence
    assert sched[0]["kind"] == "initial" and sched[0]["status"] == "due" and sched[0]["payable"]
    assert sched[0]["amount"] == 400.0  # $4/vehicle x 100
    assert sched[1]["status"] == "upcoming" and sched[1]["pay_early"] and sched[1]["payable"]
    assert sched[2]["status"] == "upcoming" and not sched[2]["payable"]

    # Provider can remind on the due installment, but not on a future one.
    assert client.post(f"/engagements/{eid}/payments/0:remind").status_code == 200
    assert client.post(f"/engagements/{eid}/payments/3:remind").status_code == 409

    # Client pays the initial installment; can't pay a far-future one.
    _as(state, Principal(user_id="client1", email="c@apex.com", groups=["client"]))
    assert client.post(f"/engagements/{eid}/payments/0:pay").status_code == 200
    assert client.post(f"/engagements/{eid}/payments/5:pay").status_code == 409
    # Client cannot send reminders (provider-only).
    assert client.post(f"/engagements/{eid}/payments/1:remind").status_code == 403

    b2 = client.get(f"/engagements/{eid}/billing").json()
    assert b2["schedule"][0]["status"] == "paid"
    assert b2["summary"]["paid_count"] == 1 and b2["summary"]["paid_amount"] == 400.0
    # Paying twice is rejected.
    assert client.post(f"/engagements/{eid}/payments/0:pay").status_code == 409


def test_users_lists_providers_and_client_only_invites(ctx):
    client, repo, state = ctx
    # A provider membership (engagement creator) surfaces in the org Users list...
    repo.put_membership(Membership(engagement_id="e1", user_id="analyst1", email="a@wheels.com",
                                   role=Role.PROVIDER, name="Ana Lyst", created_at=utcnow()))
    # ...and a client membership does not.
    repo.put_membership(Membership(engagement_id="e1", user_id="client9", email="c@apex.com",
                                   role=Role.CLIENT, created_at=utcnow()))
    users = client.get("/users").json()["users"]
    ids = {u["user_id"] for u in users}
    assert "analyst1" in ids and "client9" not in ids

    # Clients cannot list org users.
    _as(state, Principal(user_id="client9", email="c@apex.com", groups=["client"]))
    assert client.get("/users").status_code == 403


def test_illegal_transition_returns_409(ctx):
    client, repo, state = ctx
    r = client.post("/engagements", json={"name": "Apex", "client_name": "Apex LLC"})
    eid, sid = r.json()["engagement"]["engagement_id"], r.json()["submission_id"]
    # Can't submit-to-client straight from DRAFT.
    assert client.post(f"/engagements/{eid}/submissions/{sid}:submit-to-client").status_code == 409


def test_existing_customer_contact_can_be_added_to_another_engagement(ctx, monkeypatch):
    """Someone the customer already has on one engagement joins the next without a new account."""
    client, repo, state = ctx
    sent: list[dict] = []
    monkeypatch.setattr(
        "app.notify.emailer.Emailer.send",
        lambda self, **kw: sent.append(kw),
    )

    cid = client.post("/customers", json={"legal_name": "Apex Pvt Ltd"}).json()["customer"][
        "customer_id"
    ]
    first = client.post("/engagements", json={"name": "Spring lease", "customer_id": cid})
    second = client.post("/engagements", json={"name": "Autumn lease", "customer_id": cid})
    e1 = first.json()["engagement"]["engagement_id"]
    e2 = second.json()["engagement"]["engagement_id"]
    repo.put_membership(Membership(engagement_id=e1, user_id="client9", email="jordan@apex.com",
                                   role=Role.CLIENT, name="Jordan Lee", created_at=utcnow()))

    # Someone else's customer contact must never appear in this customer's picker.
    other = client.post("/customers", json={"legal_name": "Beta Corp"}).json()["customer"][
        "customer_id"
    ]
    e3 = client.post(
        "/engagements", json={"name": "Beta", "customer_id": other}
    ).json()["engagement"]["engagement_id"]
    repo.put_membership(Membership(engagement_id=e3, user_id="beta1", email="sam@beta.com",
                                   role=Role.CLIENT, created_at=utcnow()))

    # With no query the picker suggests this customer's own people, and only those.
    cands = client.get(f"/engagements/{e2}/invitations/candidates").json()["candidates"]
    assert [c["user_id"] for c in cands] == ["client9"]
    assert cands[0]["engagements"] == ["Spring lease"] and cands[0]["onboarded"] is True
    assert cands[0]["same_customer"] is True and cands[0]["customers"] == ["Apex Pvt Ltd"]

    # Searching reaches every customer-side account, each labelled with whose it is, so that
    # adding someone from another customer is a choice rather than an accident.
    found = client.get(
        f"/engagements/{e2}/invitations/candidates", params={"q": "sam@"}
    ).json()["candidates"]
    assert [c["user_id"] for c in found] == ["beta1"]
    assert found[0]["same_customer"] is False and found[0]["customers"] == ["Beta Corp"]
    # A search that matches nobody says so rather than falling back to everybody.
    assert client.get(
        f"/engagements/{e2}/invitations/candidates", params={"q": "zzz"}
    ).json()["candidates"] == []

    r = client.post(f"/engagements/{e2}/members", json={"user_id": "client9"})
    assert r.status_code == 201, r.text
    assert r.json()["membership"]["email"] == "jordan@apex.com"
    # No temporary password is issued: the account and its password already exist.
    assert sent and sent[0]["template"] == "ENGAGEMENT_ACCESS_ADDED"
    assert "temp_password" not in sent[0]

    # Now on both engagements, so no longer offered, and not addable twice.
    assert client.get(f"/engagements/{e2}/invitations/candidates").json()["candidates"] == []
    assert client.post(f"/engagements/{e2}/members", json={"user_id": "client9"}).status_code == 409
    assert "client9" in {m["user_id"] for m in client.get(f"/engagements/{e2}").json()["members"]}

    # Someone found by search is addable whichever customer they came from...
    assert client.post(f"/engagements/{e2}/members", json={"user_id": "beta1"}).status_code == 201
    # ...but Wheels staff are not customer contacts, and an unknown id is not a person.
    repo.put_membership(Membership(engagement_id=e1, user_id="analyst7", email="a7@wheels.com",
                                   role=Role.PROVIDER, name="Ana Lyst", created_at=utcnow()))
    assert client.post(f"/engagements/{e2}/members", json={"user_id": "analyst7"}).status_code == 400
    assert client.post(f"/engagements/{e2}/members", json={"user_id": "nobody"}).status_code == 404

    # And a customer cannot see who else the customer has.
    _as(state, Principal(user_id="client9", email="jordan@apex.com", groups=["client"]))
    assert client.get(f"/engagements/{e2}/invitations/candidates").status_code == 403


def test_access_email_failure_still_grants_access(ctx, monkeypatch):
    """The membership is the point; the courtesy email is not worth failing the request over."""
    client, repo, state = ctx

    def boom(self, **kw):
        raise RuntimeError("SES rejected the recipient")

    monkeypatch.setattr("app.notify.emailer.Emailer.send", boom)

    cid = client.post("/customers", json={"legal_name": "Apex Pvt Ltd"}).json()["customer"][
        "customer_id"
    ]
    e1 = client.post(
        "/engagements", json={"name": "One", "customer_id": cid}
    ).json()["engagement"]["engagement_id"]
    e2 = client.post(
        "/engagements", json={"name": "Two", "customer_id": cid}
    ).json()["engagement"]["engagement_id"]
    repo.put_membership(Membership(engagement_id=e1, user_id="client9", email="jordan@apex.com",
                                   role=Role.CLIENT, name="Jordan Lee", created_at=utcnow()))

    assert client.post(f"/engagements/{e2}/members", json={"user_id": "client9"}).status_code == 201
    assert repo.get_membership(e2, "client9") is not None


def _drive_to_audit(client, repo, state):
    """Take a fresh engagement as far as the billing audit and stop there."""
    r = client.post("/engagements", json={"name": "Apex", "client_name": "Apex LLC"})
    eid, sid = r.json()["engagement"]["engagement_id"], r.json()["submission_id"]
    did = client.post(f"/engagements/{eid}/documents:presign",
                      json={"doc_type": "MSA", "filename": "m.pdf", "submission_id": sid}).json()["document_id"]
    repo.update_submission_status(eid, sid, SubmissionStatus.DRAFT.value, SubmissionStatus.EXTRACTING.value)
    repo.update_submission_status(eid, sid, SubmissionStatus.EXTRACTING.value, SubmissionStatus.IN_UNDERWRITING.value)
    seed_pricing(repo, eid, did, 1, program="Fuel Management Program",
                 item="Fuel Fee", amount=4.0)
    client.patch(f"/engagements/{eid}/billing", json={"fleet_size": 100})
    client.post(f"/engagements/{eid}/submissions/{sid}:submit-to-client")
    repo.put_membership(Membership(engagement_id=eid, user_id="client1", email="c@apex.com",
                                   role=Role.CLIENT, created_at=utcnow()))
    _as(state, Principal(user_id="client1", email="c@apex.com", groups=["client"]))
    client.post(f"/engagements/{eid}/submissions/{sid}:client-approve",
                json={"signature": {"full_name": "Jordan Lee", "place": "Austin"}})
    _as(state, Principal(user_id="analyst1", email="a@wheels.com", groups=["provider"]))
    return eid, sid, did


def test_billing_audit_can_trace_every_charge_to_a_clause(ctx):
    """The audit is only possible if each billed line says where it was read from."""
    client, repo, state = ctx
    eid, sid, did = _drive_to_audit(client, repo, state)

    body = client.get(f"/engagements/{eid}/billing", params={"submission_id": sid}).json()
    assert body["status"] == "PENDING_BILLING_AUDIT"
    item = next(i for i in body["config"]["pricing_items"] if i["item"] == "Fuel Fee")
    # Without these three the right-hand pane has a number and no way to check it.
    assert item["document_id"] == did
    assert item["version"] == 1
    assert item["citations"] and item["citations"][0]["page"] == 1
    assert item["record_id"]

    # And the schedule is generated *before* the audit, because what is being audited is what
    # the customer will be invoiced — dates included.
    assert body["schedule"] and body["monthly_recurring"] == 400.0
    # The screen's own arithmetic must land on the stored figure, or it warns instead of
    # approving: per-vehicle-per-month amount × fleet.
    assert item["amount"] * body["fleet_size"] == body["monthly_recurring"]


def test_billing_audit_reads_the_cycle_under_audit_not_the_one_in_force(ctx):
    """During an amendment the billing *in force* is the previous cycle, which is not the
    thing being audited."""
    client, repo, state = ctx
    eid = _drive_to_active(client, repo, state, None)
    live_sid = client.get(f"/engagements/{eid}").json()["submission"]["submission_id"]

    amendment = client.post(f"/engagements/{eid}/amendments").json()["submission"]
    new_sid = amendment["submission_id"]
    assert new_sid != live_sid

    # Unasked, billing answers with the cycle that is actually billing today.
    assert client.get(f"/engagements/{eid}/billing").json()["status"] == "ACTIVE"
    # Named, it answers with the cycle the audit is looking at.
    assert client.get(
        f"/engagements/{eid}/billing", params={"submission_id": new_sid}
    ).json()["status"] == "DRAFT"


def test_the_audit_corrects_a_charge_in_place_and_the_billing_follows(ctx):
    """A misread amount is fixed where it is seen, and everything downstream reflows."""
    client, repo, state = ctx
    eid, sid, did = _drive_to_audit(client, repo, state)
    before = client.get(f"/engagements/{eid}/billing", params={"submission_id": sid}).json()
    assert before["monthly_recurring"] == 400.0  # $4.00 x 100
    item = next(i for i in before["config"]["pricing_items"] if i["item"] == "Fuel Fee")

    r = client.patch(f"/engagements/{eid}/billing/items/{item['record_id']}", json={"amount": 3.5})
    assert r.status_code == 200
    after = r.json()

    fixed = next(i for i in after["config"]["pricing_items"] if i["item"] == "Fuel Fee")
    assert fixed["amount"] == 3.5
    # The reading is kept beside the correction: the citation still proves what the contract
    # said, and the screen can show that the two differ.
    assert fixed["as_read"]["amount"] == 4.0
    assert fixed["corrected"] == ["amount"]
    assert fixed["citations"] == item["citations"]
    # And what will actually be charged has moved with it, schedule included.
    assert after["monthly_recurring"] == 350.0
    assert all(row["amount"] == 350.0 for row in after["schedule"] if not row.get("paid_at"))

    # It survives a rebuild, because the override lives on the term rather than on the config.
    again = client.get(f"/engagements/{eid}/billing", params={"submission_id": sid}).json()
    assert again["monthly_recurring"] == 350.0

    # Approving takes it live at the corrected figure.
    assert client.post(
        f"/engagements/{eid}/submissions/{sid}:approve-billing"
    ).json()["submission"]["status"] == "ACTIVE"
    assert repo.get_engagement(eid).monthly_recurring == 350.0


def test_the_audit_can_move_a_charge_out_of_recurring(ctx):
    """"Billed per vehicle per month" read off a clause that meant per claim is the error that
    matters most: it multiplies by the fleet."""
    client, repo, state = ctx
    eid, sid, _ = _drive_to_audit(client, repo, state)
    item = next(
        i for i in client.get(f"/engagements/{eid}/billing", params={"submission_id": sid})
        .json()["config"]["pricing_items"] if i["item"] == "Fuel Fee"
    )
    after = client.patch(
        f"/engagements/{eid}/billing/items/{item['record_id']}", json={"billing_class": "usage"}
    ).json()
    moved = next(i for i in after["config"]["pricing_items"] if i["item"] == "Fuel Fee")
    assert moved["billing_class"] == "usage"
    # The basis has to move with the class, or the estimate would keep counting it.
    assert "per_vehicle_per_month" not in moved["unit_basis"]
    assert after["monthly_recurring"] == 0.0


def test_billing_cannot_be_corrected_once_it_is_live(ctx):
    """After go-live the figures are what a customer is being invoiced against."""
    client, repo, state = ctx
    eid = _drive_to_active(client, repo, state, None)
    item = client.get(f"/engagements/{eid}/billing").json()["config"]["pricing_items"][0]
    r = client.patch(f"/engagements/{eid}/billing/items/{item['record_id']}", json={"amount": 1.0})
    assert r.status_code == 409


def test_a_client_cannot_correct_the_billing(ctx):
    client, repo, state = ctx
    eid, sid, _ = _drive_to_audit(client, repo, state)
    item = client.get(f"/engagements/{eid}/billing",
                      params={"submission_id": sid}).json()["config"]["pricing_items"][0]
    _as(state, Principal(user_id="client1", email="c@apex.com", groups=["client"]))
    r = client.patch(f"/engagements/{eid}/billing/items/{item['record_id']}", json={"amount": 1.0})
    assert r.status_code == 403


def test_waiting_is_measured_from_the_last_move_not_the_handshake(ctx):
    """An engagement opened long ago and signed yesterday has been waiting a day.

    The lifecycle board read `created_at`, so a year-old deal that moved this morning showed as
    365 days waiting and sent somebody chasing it."""
    client, repo, state = ctx
    r = client.post("/engagements", json={"name": "Apex", "client_name": "Apex LLC"})
    eid, sid = r.json()["engagement"]["engagement_id"], r.json()["submission_id"]
    # Backdate the engagement itself; the submission keeps moving in the present.
    repo.table.update_item(
        Key={"PK": f"ENG#{eid}", "SK": "#META"},
        UpdateExpression="SET created_at = :c",
        ExpressionAttributeValues={":c": "2020-01-01T00:00:00+00:00"},
    )
    repo.update_submission_status(
        eid, sid, SubmissionStatus.DRAFT.value, SubmissionStatus.EXTRACTING.value
    )

    row = next(
        e for e in client.get("/engagements").json()["engagements"]
        if e["engagement_id"] == eid
    )
    assert row["created_at"].startswith("2020")
    assert row["status_since"] and not row["status_since"].startswith("2020")
    assert row["status"] == "EXTRACTING"
