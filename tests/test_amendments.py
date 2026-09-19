"""Amendments: a second review cycle over an engagement that is already live.

The behaviour under test is the one that makes an amendment safe to start: opening one must
not disturb the deal the customer signed. The engagement keeps billing, keeps its ACTIVE
status and keeps its place on the finance dashboard until the new terms have been through the
same approval path the original terms took.
"""

from __future__ import annotations

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app.auth.deps import get_principal, get_repo, get_s3
from app.auth.principal import Principal
from app.lifecycle.amendment import can_open_amendment, cycle_label
from app.lifecycle.submission_state import Role, SubmissionStatus
from app.main import app
from app.store.models import Membership
from app.store.repository import Repository, utcnow
from app.store.s3 import S3Store

from .pricing_helpers import seed_pricing
from .test_api import REGION, _as, _drive_to_active, _make_table, _make_vehicles_table


@pytest.fixture
def ctx():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        _make_table(ddb)
        _make_vehicles_table(ddb)
        s3c = boto3.client("s3", region_name=REGION)
        s3c.create_bucket(
            Bucket="wheels-test-docs",
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        repo = Repository(
            table_name="wheels-test", resource=ddb, vehicles_table_name="wheels-test-vehicles"
        )
        s3 = S3Store(bucket="wheels-test-docs", client=s3c)
        state = {
            "principal": Principal(user_id="analyst1", email="a@wheels.com", groups=["provider"])
        }
        app.dependency_overrides[get_repo] = lambda: repo
        app.dependency_overrides[get_s3] = lambda: s3
        app.dependency_overrides[get_principal] = lambda: state["principal"]
        yield TestClient(app), repo, state
        app.dependency_overrides.clear()


def test_cycle_labels_name_the_original_deal_and_each_amendment():
    assert cycle_label(1) == "Original agreement"
    assert cycle_label(2) == "Amendment 1"
    assert cycle_label(3) == "Amendment 2"


def test_only_a_completed_cycle_can_be_amended():
    assert can_open_amendment(SubmissionStatus.ACTIVE) is True
    assert can_open_amendment("ACTIVE") is True
    for status in ("DRAFT", "IN_UNDERWRITING", "PENDING_CLIENT_APPROVAL", "PENDING_BILLING_AUDIT"):
        assert can_open_amendment(status) is False, status
    assert can_open_amendment(None) is False


def test_opening_an_amendment_starts_a_second_cycle_from_the_agreements_in_force(ctx):
    client, repo, state = ctx
    eid = _drive_to_active(client, repo, state, None)
    original = repo.current_submission(eid)
    assert original.cycle == 1 and original.status == SubmissionStatus.ACTIVE

    r = client.post(f"/engagements/{eid}/amendments")
    assert r.status_code == 201, r.text
    assert r.json()["cycle"] == 2 and r.json()["label"] == "Amendment 1"

    cycles = repo.list_submissions(eid)
    assert [c.cycle for c in cycles] == [1, 2]
    # The completed cycle is untouched: same status, same signature, same terms.
    assert repo.get_submission(eid, original.submission_id).status == SubmissionStatus.ACTIVE
    # The amendment starts where the deal stands, not from nothing.
    amendment = repo.current_submission(eid)
    assert amendment.cycle == 2 and amendment.status == SubmissionStatus.DRAFT
    assert amendment.docs() == original.docs()
    # The live cycle is still the one holding the terms in force.
    assert repo.live_submission(eid).submission_id == original.submission_id


def test_an_engagement_stays_live_while_its_amendment_is_reviewed(ctx):
    """The point of a separate cycle: billing does not stop to wait for the new terms."""
    client, repo, state = ctx
    eid = _drive_to_active(client, repo, state, None)
    monthly_before = repo.get_engagement(eid).monthly_recurring

    sid = client.post(f"/engagements/{eid}/amendments").json()["submission"]["submission_id"]
    repo.update_submission_status(
        eid, sid, SubmissionStatus.DRAFT.value, SubmissionStatus.EXTRACTING.value
    )
    repo.update_submission_status(
        eid, sid, SubmissionStatus.EXTRACTING.value, SubmissionStatus.IN_UNDERWRITING.value
    )

    # The amendment is in underwriting; the engagement is still a live, billing engagement.
    assert repo.get_engagement(eid).status == "ACTIVE"
    assert repo.get_engagement(eid).monthly_recurring == monthly_before
    dash = client.get("/finance/dashboard").json()
    assert dash["totals"]["active"] == 1
    assert next(r for r in dash["engagements"] if r["engagement_id"] == eid)["status"] == "ACTIVE"
    # Fleet size stays locked, because the schedule in force still prices off it.
    assert client.patch(f"/engagements/{eid}/billing", json={"fleet_size": 250}).status_code == 409


def test_the_amendment_takes_over_once_it_goes_live(ctx):
    client, repo, state = ctx
    eid = _drive_to_active(client, repo, state, None)
    sid = client.post(f"/engagements/{eid}/amendments").json()["submission"]["submission_id"]

    did = client.post(
        f"/engagements/{eid}/documents:presign",
        json={"filename": "renewal.pdf", "submission_id": sid, "doc_type": "MSA"},
    ).json()["document_id"]
    repo.update_submission_status(
        eid, sid, SubmissionStatus.DRAFT.value, SubmissionStatus.EXTRACTING.value
    )
    repo.update_submission_status(
        eid, sid, SubmissionStatus.EXTRACTING.value, SubmissionStatus.IN_UNDERWRITING.value
    )
    # The renewal doubles the fuel rate.
    seed_pricing(repo, eid, did, 1, program="Fuel Management Program",
                  item="Fuel Fee", amount=8.0)

    client.post(f"/engagements/{eid}/submissions/{sid}:submit-to-client")
    repo.put_membership(Membership(
        engagement_id=eid, user_id="client1", email="c@apex.com",
        role=Role.CLIENT, created_at=utcnow()))
    _as(state, Principal(user_id="client1", email="c@apex.com", groups=["client"]))
    client.post(
        f"/engagements/{eid}/submissions/{sid}:client-approve",
        json={"signature": {"full_name": "Jordan Lee", "place": "Austin"}},
    )
    _as(state, Principal(user_id="analyst1", email="a@wheels.com", groups=["provider"]))
    assert client.post(
        f"/engagements/{eid}/submissions/{sid}:approve-billing"
    ).json()["submission"]["status"] == "ACTIVE"

    # Cycle 2 is now the one in force, and the new rate is what bills.
    assert repo.live_submission(eid).cycle == 2
    assert repo.get_engagement(eid).status == "ACTIVE"
    assert repo.get_engagement(eid).monthly_recurring == 800.0  # $8 * 100 vehicles

    # Unpaid installments reprice off the engagement's current dues rather than being re-dated.
    schedule = client.get(f"/engagements/{eid}/billing").json()["schedule"]
    assert schedule and all(row["amount"] == 800.0 for row in schedule if row["status"] != "paid")


def test_a_second_amendment_cannot_start_while_one_is_open(ctx):
    client, repo, state = ctx
    eid = _drive_to_active(client, repo, state, None)
    assert client.post(f"/engagements/{eid}/amendments").status_code == 201
    r = client.post(f"/engagements/{eid}/amendments")
    assert r.status_code == 409
    assert "already has a review cycle in progress" in r.json()["detail"]


def test_a_live_engagement_refuses_an_upload_and_says_to_amend(ctx):
    client, repo, state = ctx
    eid = _drive_to_active(client, repo, state, None)
    sub = repo.current_submission(eid)

    r = client.post(
        f"/engagements/{eid}/documents:presign",
        json={"filename": "renewal.pdf", "submission_id": sub.submission_id},
    )
    assert r.status_code == 409
    assert "open an amendment" in r.json()["detail"]

    # Once the amendment is open, the same upload is accepted and stamped with its cycle.
    sid = client.post(f"/engagements/{eid}/amendments").json()["submission"]["submission_id"]
    r = client.post(
        f"/engagements/{eid}/documents:presign",
        json={"filename": "renewal.pdf", "submission_id": sid},
    )
    assert r.status_code == 201
    assert repo.get_document(eid, r.json()["document_id"]).cycle == 2


def test_mid_review_uploads_are_refused_so_nothing_shifts_under_the_customer(ctx):
    client, repo, state = ctx
    r = client.post("/engagements", json={"name": "Apex", "client_name": "Apex LLC"})
    eid, sid = r.json()["engagement"]["engagement_id"], r.json()["submission_id"]
    client.post(
        f"/engagements/{eid}/documents:presign",
        json={"doc_type": "MSA", "filename": "m.pdf", "submission_id": sid},
    )
    repo.update_submission_status(
        eid, sid, SubmissionStatus.DRAFT.value, SubmissionStatus.EXTRACTING.value
    )
    # Extraction is running: an upload now would race the worker reading the same cycle.
    assert client.post(
        f"/engagements/{eid}/documents:presign",
        json={"filename": "x.pdf", "submission_id": sid},
    ).status_code == 409

    repo.update_submission_status(
        eid, sid, SubmissionStatus.EXTRACTING.value, SubmissionStatus.IN_UNDERWRITING.value
    )
    # Under analyst review, more documents are welcome.
    assert client.post(
        f"/engagements/{eid}/documents:presign",
        json={"filename": "x.pdf", "submission_id": sid},
    ).status_code == 201

    client.post(f"/engagements/{eid}/submissions/{sid}:submit-to-client")
    # With the customer, they are not.
    assert client.post(
        f"/engagements/{eid}/documents:presign",
        json={"filename": "y.pdf", "submission_id": sid},
    ).status_code == 409


def test_a_client_may_not_open_an_amendment(ctx):
    client, repo, state = ctx
    eid = _drive_to_active(client, repo, state, None)
    repo.put_membership(Membership(
        engagement_id=eid, user_id="client1", email="c@apex.com",
        role=Role.CLIENT, created_at=utcnow()))
    _as(state, Principal(user_id="client1", email="c@apex.com", groups=["client"]))
    assert client.post(f"/engagements/{eid}/amendments").status_code == 403
    # And the engagement does not offer it to them.
    assert client.get(f"/engagements/{eid}").json()["can_open_amendment"] is False


def test_the_engagement_response_describes_its_cycles(ctx):
    client, repo, state = ctx
    eid = _drive_to_active(client, repo, state, None)
    body = client.get(f"/engagements/{eid}").json()
    assert body["can_open_amendment"] is True
    assert body["is_amendment"] is False
    assert [c["label"] for c in body["cycles"]] == ["Original agreement"]

    client.post(f"/engagements/{eid}/amendments")
    body = client.get(f"/engagements/{eid}").json()
    assert body["is_amendment"] is True
    assert body["cycle_label"] == "Amendment 1"
    assert body["can_open_amendment"] is False  # one is already open
    assert [c["label"] for c in body["cycles"]] == ["Original agreement", "Amendment 1"]
    # `submission` is the amendment; `live_submission_id` is still the signed cycle.
    assert body["submission"]["cycle"] == 2
    assert body["live_submission_id"] == repo.live_submission(eid).submission_id


def test_a_customer_is_told_an_amendment_opened_but_not_how_it_is_processed(ctx):
    client, repo, state = ctx
    eid = _drive_to_active(client, repo, state, None)
    client.post(f"/engagements/{eid}/amendments")
    sub = repo.current_submission(eid)
    client.post(
        f"/engagements/{eid}/documents:presign",
        json={"filename": "renewal.pdf", "submission_id": sub.submission_id},
    )

    repo.put_membership(Membership(
        engagement_id=eid, user_id="client1", email="c@apex.com",
        role=Role.CLIENT, created_at=utcnow()))
    _as(state, Principal(user_id="client1", email="c@apex.com", groups=["client"]))
    actions = [e["action"] for e in client.get(f"/engagements/{eid}/audit").json()["events"]]
    assert "amendment_opened" in actions
    assert "field_approved" not in actions


def test_an_amendment_opened_by_mistake_can_be_discarded(ctx):
    """Without this an accidental click blocks the engagement: no second cycle may open."""
    client, repo, state = ctx
    eid = _drive_to_active(client, repo, state, None)
    sid = client.post(f"/engagements/{eid}/amendments").json()["submission"]["submission_id"]
    assert client.get(f"/engagements/{eid}").json()["can_open_amendment"] is False

    r = client.delete(f"/engagements/{eid}/amendments/{sid}")
    assert r.status_code == 200 and r.json()["discarded_cycle"] == 2
    body = client.get(f"/engagements/{eid}")
    assert body.json()["can_open_amendment"] is True
    assert [c["label"] for c in body.json()["cycles"]] == ["Original agreement"]
    # The live cycle is untouched and the engagement never left ACTIVE.
    assert repo.live_submission(eid).cycle == 1
    assert repo.get_engagement(eid).status == "ACTIVE"
    # A fresh amendment numbers from the cycles that remain.
    assert client.post(f"/engagements/{eid}/amendments").json()["cycle"] == 2


def test_an_amendment_with_work_in_it_is_not_discarded_silently(ctx):
    client, repo, state = ctx
    eid = _drive_to_active(client, repo, state, None)
    sid = client.post(f"/engagements/{eid}/amendments").json()["submission"]["submission_id"]
    did = client.post(
        f"/engagements/{eid}/documents:presign",
        json={"filename": "renewal.pdf", "submission_id": sid},
    ).json()["document_id"]

    r = client.delete(f"/engagements/{eid}/amendments/{sid}")
    assert r.status_code == 409
    assert "remove the agreements" in r.json()["detail"]

    # Removing the document makes it discardable again.
    assert client.delete(f"/engagements/{eid}/documents/{did}").status_code == 200
    assert client.delete(f"/engagements/{eid}/amendments/{sid}").status_code == 200


def test_the_signed_cycle_is_never_discardable(ctx):
    client, repo, state = ctx
    eid = _drive_to_active(client, repo, state, None)
    original = repo.live_submission(eid).submission_id
    r = client.delete(f"/engagements/{eid}/amendments/{original}")
    assert r.status_code == 409
    assert "original review cycle" in r.json()["detail"]
    assert repo.live_submission(eid).submission_id == original


def test_an_amendment_under_review_is_not_discardable(ctx):
    client, repo, state = ctx
    eid = _drive_to_active(client, repo, state, None)
    sid = client.post(f"/engagements/{eid}/amendments").json()["submission"]["submission_id"]
    repo.update_submission_status(
        eid, sid, SubmissionStatus.DRAFT.value, SubmissionStatus.EXTRACTING.value
    )
    r = client.delete(f"/engagements/{eid}/amendments/{sid}")
    assert r.status_code == 409
    assert "before it starts" in r.json()["detail"]
