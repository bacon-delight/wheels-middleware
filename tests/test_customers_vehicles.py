"""Customers parent engagements; vehicles are real records mapped onto those engagements."""

from __future__ import annotations

import pytest

from app.auth.principal import Principal
from app.store.models import VehicleOwnership, VehicleStatus

from .test_api import _as, ctx  # noqa: F401 - `ctx` is the shared moto fixture


def _customer(client, name="Apex Pvt Ltd"):
    r = client.post("/customers", json={"legal_name": name, "industry": "Logistics"})
    assert r.status_code == 201, r.text
    return r.json()["customer"]["customer_id"]


def _vehicle(client, **kw):
    body = {"body_class": "PICKUP", "make": "Ford", "model": "F-150", **kw}
    r = client.post("/vehicles", json=body)
    assert r.status_code == 201, r.text
    return r.json()["vehicle"]


def test_one_customer_holds_many_engagements(ctx):  # noqa: F811
    """The core shape: lease 40 now, 20 more later, one customer, two engagements."""
    client, repo, state = ctx
    cid = _customer(client)
    first = client.post("/engagements", json={"name": "Spring lease", "customer_id": cid})
    second = client.post("/engagements", json={"name": "Autumn lease", "customer_id": cid})
    assert first.status_code == 201 and second.status_code == 201

    detail = client.get(f"/customers/{cid}").json()
    assert {e["name"] for e in detail["engagements"]} == {"Spring lease", "Autumn lease"}
    # client_name is denormalized from the customer so lists need no join.
    assert first.json()["engagement"]["client_name"] == "Apex Pvt Ltd"

    listing = client.get("/customers").json()["customers"]
    assert listing[0]["engagement_count"] == 2


def test_duplicate_customer_is_rejected(ctx):  # noqa: F811
    client, repo, state = ctx
    _customer(client)
    assert client.post("/customers", json={"legal_name": "apex pvt ltd"}).status_code == 409


def test_engagement_requires_a_customer_or_a_name(ctx):  # noqa: F811
    client, repo, state = ctx
    assert client.post("/engagements", json={"name": "X"}).status_code == 400
    assert client.post("/engagements", json={"name": "X", "customer_id": "nope"}).status_code == 404


def test_renaming_a_customer_updates_its_engagements(ctx):  # noqa: F811
    client, repo, state = ctx
    cid = _customer(client)
    eid = client.post(
        "/engagements", json={"name": "E", "customer_id": cid}
    ).json()["engagement"]["engagement_id"]
    client.patch(f"/customers/{cid}", json={"legal_name": "Apex Global Ltd"})
    assert client.get(f"/engagements/{eid}").json()["engagement"]["client_name"] == "Apex Global Ltd"


def test_assigning_vehicles_drives_the_derived_fleet_size(ctx):  # noqa: F811
    client, repo, state = ctx
    cid = _customer(client)
    eid = client.post(
        "/engagements", json={"name": "Spring lease", "customer_id": cid}
    ).json()["engagement"]["engagement_id"]
    assert client.get(f"/engagements/{eid}").json()["assigned_vehicle_count"] == 0

    ids = [_vehicle(client, unit_number=f"U{i}")["vehicle_id"] for i in range(3)]
    r = client.post("/vehicles:bulk-assign", json={"vehicle_ids": ids, "engagement_id": eid})
    assert r.status_code == 200 and r.json()["assigned_vehicle_count"] == 3

    detail = client.get(f"/engagements/{eid}").json()
    assert detail["assigned_vehicle_count"] == 3
    assert detail["engagement"]["fleet_size"] == 3
    assert detail["fleet_size_source"] == "derived"

    # Releasing one brings the billed fleet back down.
    client.post(f"/vehicles/{ids[0]}:release")
    assert client.get(f"/engagements/{eid}").json()["engagement"]["fleet_size"] == 2


def test_override_beats_the_assigned_count_until_cleared(ctx):  # noqa: F811
    client, repo, state = ctx
    cid = _customer(client)
    eid = client.post(
        "/engagements", json={"name": "E", "customer_id": cid}
    ).json()["engagement"]["engagement_id"]
    vid = _vehicle(client)["vehicle_id"]
    client.post(f"/vehicles/{vid}:assign", json={"engagement_id": eid})

    b = client.patch(f"/engagements/{eid}/billing", json={"fleet_size": 40}).json()
    assert b["fleet_size"] == 40 and b["assigned_vehicle_count"] == 1
    assert b["fleet_size_source"] == "override"

    b = client.patch(f"/engagements/{eid}/billing", json={"fleet_size": None}).json()
    assert b["fleet_size"] == 1 and b["fleet_size_source"] == "derived"


def test_a_vehicle_cannot_be_assigned_twice(ctx):  # noqa: F811
    client, repo, state = ctx
    cid = _customer(client)
    a = client.post("/engagements", json={"name": "A", "customer_id": cid}).json()
    b = client.post("/engagements", json={"name": "B", "customer_id": cid}).json()
    vid = _vehicle(client)["vehicle_id"]
    client.post(f"/vehicles/{vid}:assign", json={"engagement_id": a["engagement"]["engagement_id"]})
    r = client.post(
        f"/vehicles/{vid}:assign", json={"engagement_id": b["engagement"]["engagement_id"]}
    )
    assert r.status_code == 409


def test_customer_owned_vehicles_are_never_available_to_lease(ctx):  # noqa: F811
    """Third-party units: Wheels sells services, not the asset, so they never count as stock."""
    client, repo, state = ctx
    cid = _customer(client)
    owned = _vehicle(client, unit_number="ours")
    third = _vehicle(
        client, unit_number="theirs", ownership=VehicleOwnership.CUSTOMER_OWNED.value,
        customer_id=cid,
    )
    assert owned["status"] == VehicleStatus.IN_STOCK.value
    assert third["status"] == VehicleStatus.ON_ORDER.value

    summary = client.get("/vehicles/summary").json()
    assert summary["total"] == 2
    assert summary["available_to_lease"] == 1
    assert summary["customer_owned"] == 1

    # Forcing one into stock is refused outright.
    bad = client.post("/vehicles", json={
        "body_class": "SEDAN", "ownership": VehicleOwnership.CUSTOMER_OWNED.value,
        "customer_id": cid, "status": VehicleStatus.IN_STOCK.value,
    })
    assert bad.status_code == 400


def test_customer_owned_vehicle_requires_a_customer(ctx):  # noqa: F811
    client, repo, state = ctx
    r = client.post("/vehicles", json={
        "body_class": "SEDAN", "ownership": VehicleOwnership.CUSTOMER_OWNED.value,
    })
    assert r.status_code == 400


def test_duty_band_is_derived_from_the_body_class(ctx):  # noqa: F811
    client, repo, state = ctx
    assert _vehicle(client, body_class="SEDAN")["duty_band"] == "LIGHT_DUTY"
    assert _vehicle(client, body_class="BOX_TRUCK")["duty_band"] == "MEDIUM_DUTY"
    assert _vehicle(client, body_class="TRACTOR")["duty_band"] == "HEAVY_DUTY"
    assert _vehicle(client, body_class="FORKLIFT")["duty_band"] == "EQUIPMENT"


def test_inventory_filters_by_status_and_duty_band(ctx):  # noqa: F811
    client, repo, state = ctx
    _vehicle(client, body_class="SEDAN")
    _vehicle(client, body_class="TRACTOR")
    heavy = client.get("/vehicles", params={"duty_band": "HEAVY_DUTY"}).json()
    assert heavy["count"] == 1 and heavy["vehicles"][0]["body_class"] == "TRACTOR"
    in_stock = client.get("/vehicles", params={"status": "IN_STOCK"}).json()
    assert in_stock["count"] == 2


def test_clients_may_see_only_their_own_engagement_vehicles(ctx):  # noqa: F811
    client, repo, state = ctx
    from app.lifecycle.submission_state import Role
    from app.store.models import Membership
    from app.store.repository import utcnow

    cid = _customer(client)
    eid = client.post(
        "/engagements", json={"name": "E", "customer_id": cid}
    ).json()["engagement"]["engagement_id"]
    vid = _vehicle(client)["vehicle_id"]
    client.post(f"/vehicles/{vid}:assign", json={"engagement_id": eid})
    repo.put_membership(Membership(
        engagement_id=eid, user_id="client1", email="c@apex.com", role=Role.CLIENT,
        created_at=utcnow(),
    ))

    _as(state, Principal(user_id="client1", email="c@apex.com", groups=["client"]))
    mine = client.get(f"/engagements/{eid}/vehicles")
    assert mine.status_code == 200 and mine.json()["assigned_vehicle_count"] == 1
    # The org-wide inventory stays provider-only.
    assert client.get("/vehicles").status_code == 403
    assert client.get("/vehicles/summary").status_code == 403


@pytest.mark.parametrize(
    "scope,allowed,refused",
    [("LEASE_ONLY", "MLA", "MSA"), ("SERVICE_ONLY", "MSA", "MLA")],
)
def test_scope_limits_which_agreements_can_be_uploaded(ctx, scope, allowed, refused):  # noqa: F811
    client, repo, state = ctx
    cid = _customer(client)
    r = client.post("/engagements", json={"name": "E", "customer_id": cid, "scope": scope}).json()
    eid, sid = r["engagement"]["engagement_id"], r["submission_id"]

    detail = client.get(f"/engagements/{eid}").json()
    assert detail["required_doc_types"] == [allowed]
    assert detail["missing_doc_types"] == [allowed]

    ok = client.post(f"/engagements/{eid}/documents:presign",
                     json={"doc_type": allowed, "filename": "a.pdf", "submission_id": sid})
    assert ok.status_code == 201
    bad = client.post(f"/engagements/{eid}/documents:presign",
                      json={"doc_type": refused, "filename": "b.pdf", "submission_id": sid})
    assert bad.status_code == 400

    # One agreement is now the complete set, so extraction may start.
    assert client.get(f"/engagements/{eid}").json()["missing_doc_types"] == []
    started = client.post(f"/engagements/{eid}/submissions/{sid}:submit-for-processing")
    assert started.status_code == 200


def test_extraction_is_refused_while_a_required_agreement_is_missing(ctx):  # noqa: F811
    client, repo, state = ctx
    cid = _customer(client)
    r = client.post("/engagements", json={
        "name": "E", "customer_id": cid, "scope": "LEASE_AND_SERVICE",
    }).json()
    eid, sid = r["engagement"]["engagement_id"], r["submission_id"]
    client.post(f"/engagements/{eid}/documents:presign",
                json={"doc_type": "MLA", "filename": "a.pdf", "submission_id": sid})

    blocked = client.post(f"/engagements/{eid}/submissions/{sid}:submit-for-processing")
    assert blocked.status_code == 400 and "MSA" in blocked.json()["detail"]

    client.post(f"/engagements/{eid}/documents:presign",
                json={"doc_type": "MSA", "filename": "b.pdf", "submission_id": sid})
    assert client.post(
        f"/engagements/{eid}/submissions/{sid}:submit-for-processing"
    ).status_code == 200


def test_scope_locks_once_the_client_has_been_asked_to_approve(ctx):  # noqa: F811
    client, repo, state = ctx
    from app.lifecycle.submission_state import SubmissionStatus

    cid = _customer(client)
    r = client.post("/engagements", json={"name": "E", "customer_id": cid}).json()
    eid, sid = r["engagement"]["engagement_id"], r["submission_id"]
    assert client.patch(f"/engagements/{eid}", json={"scope": "LEASE_ONLY"}).status_code == 200

    for a, b in (
        (SubmissionStatus.DRAFT, SubmissionStatus.EXTRACTING),
        (SubmissionStatus.EXTRACTING, SubmissionStatus.IN_UNDERWRITING),
        (SubmissionStatus.IN_UNDERWRITING, SubmissionStatus.PENDING_CLIENT_APPROVAL),
    ):
        repo.update_submission_status(eid, sid, a.value, b.value)
    assert client.patch(f"/engagements/{eid}", json={"scope": "SERVICE_ONLY"}).status_code == 409
