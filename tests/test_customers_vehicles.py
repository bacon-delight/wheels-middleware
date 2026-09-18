"""Customers parent engagements; vehicles are real records mapped onto those engagements."""

from __future__ import annotations

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


def test_scope_is_derived_from_the_agreements_uploaded(ctx):  # noqa: F811
    """Nobody declares the scope any more; it follows whatever agreements are in force."""
    client, repo, state = ctx
    cid = _customer(client)
    r = client.post("/engagements", json={"name": "E", "customer_id": cid}).json()
    eid, sid = r["engagement"]["engagement_id"], r["submission_id"]
    assert client.get(f"/engagements/{eid}").json()["engagement"]["scope"] is None

    # An uploader may state the type; otherwise the parse worker reads it off the text.
    client.post(f"/engagements/{eid}/documents:presign",
                json={"doc_type": "MLA", "filename": "a.pdf", "submission_id": sid})
    assert client.get(f"/engagements/{eid}").json()["engagement"]["scope"] == "LEASE_ONLY"

    client.post(f"/engagements/{eid}/documents:presign",
                json={"doc_type": "MSA", "filename": "b.pdf", "submission_id": sid})
    d = client.get(f"/engagements/{eid}").json()
    assert d["engagement"]["scope"] == "LEASE_AND_SERVICE"
    assert d["required_doc_types"] == ["MLA", "MSA"]


def test_a_newer_agreement_supersedes_the_older_one_of_its_type(ctx):  # noqa: F811
    """Customers bring old agreements for the record; only the newest of a type governs."""
    client, repo, state = ctx
    from app.validation.reconcile import reconcile_engagement

    cid = _customer(client)
    r = client.post("/engagements", json={"name": "E", "customer_id": cid}).json()
    eid, sid = r["engagement"]["engagement_id"], r["submission_id"]

    old_doc = client.post(f"/engagements/{eid}/documents:presign", json={
        "doc_type": "MLA", "filename": "MLA_2021.pdf", "submission_id": sid,
    }).json()["document_id"]
    new_doc = client.post(f"/engagements/{eid}/documents:presign", json={
        "doc_type": "MLA", "filename": "MLA_2025.pdf", "submission_id": sid,
    }).json()["document_id"]
    assert old_doc != new_doc, "a second agreement of the same type is its own document"

    repo.set_document_meta(eid, old_doc, effective_date="2021-01-01")
    repo.set_document_meta(eid, new_doc, effective_date="2025-01-01")
    reconcile_engagement(repo, eid)

    docs = {d["document_id"]: d for d in client.get(f"/engagements/{eid}").json()["documents"]}
    assert docs[new_doc]["standing"] == "CURRENT"
    assert docs[old_doc]["standing"] == "SUPERSEDED"
    # Only the agreement in force is under negotiation.
    assert repo.list_submissions(eid)[0].docs() == {"MLA": new_doc}


def test_extraction_needs_at_least_one_agreement(ctx):  # noqa: F811
    client, repo, state = ctx
    cid = _customer(client)
    r = client.post("/engagements", json={"name": "E", "customer_id": cid}).json()
    eid, sid = r["engagement"]["engagement_id"], r["submission_id"]

    blocked = client.post(f"/engagements/{eid}/submissions/{sid}:submit-for-processing")
    assert blocked.status_code == 400 and "at least one" in blocked.json()["detail"]

    client.post(f"/engagements/{eid}/documents:presign",
                json={"doc_type": "MLA", "filename": "a.pdf", "submission_id": sid})
    assert client.post(
        f"/engagements/{eid}/submissions/{sid}:submit-for-processing"
    ).status_code == 200


def test_extraction_runs_on_a_document_whose_type_is_not_yet_known(ctx):  # noqa: F811
    """Classification happens during extraction, so requiring a typed document first would
    deadlock: nothing could ever be classified."""
    client, repo, state = ctx
    cid = _customer(client)
    r = client.post("/engagements", json={"name": "E", "customer_id": cid}).json()
    eid, sid = r["engagement"]["engagement_id"], r["submission_id"]

    # No doc_type given, so it lands as UNKNOWN and fills no submission slot.
    client.post(f"/engagements/{eid}/documents:presign",
                json={"filename": "Walmart-SOW.pdf", "submission_id": sid})
    assert repo.list_submissions(eid)[0].docs() == {}
    assert client.get(f"/engagements/{eid}").json()["engagement"]["scope"] is None

    assert client.post(
        f"/engagements/{eid}/submissions/{sid}:submit-for-processing"
    ).status_code == 200


def test_unclassified_documents_do_not_supersede_each_other(ctx):  # noqa: F811
    """Two unreadable uploads are not competing editions of one agreement."""
    client, repo, state = ctx
    from app.validation.reconcile import reconcile_engagement

    cid = _customer(client)
    r = client.post("/engagements", json={"name": "E", "customer_id": cid}).json()
    eid, sid = r["engagement"]["engagement_id"], r["submission_id"]
    for fn in ("sow.pdf", "amendment.pdf"):
        client.post(f"/engagements/{eid}/documents:presign",
                    json={"filename": fn, "submission_id": sid})
    reconcile_engagement(repo, eid)

    standings = {d["filename"]: d["standing"]
                 for d in client.get(f"/engagements/{eid}").json()["documents"]}
    assert set(standings.values()) == {"CURRENT"}


def test_a_submission_advances_even_when_no_document_could_be_typed(ctx):  # noqa: F811
    """The stall this guards against: an engagement whose only upload is a statement of work
    finished its pipeline but never left EXTRACTING, because the guard counted typed slots and
    an unidentifiable document never fills one."""
    client, repo, state = ctx
    from app.lifecycle.submission_state import SubmissionStatus
    from app.pipeline.extract import _all_documents_extracted
    from app.store.models import DocumentVersion
    from app.store.repository import utcnow

    cid = _customer(client)
    r = client.post("/engagements", json={"name": "E", "customer_id": cid}).json()
    eid, sid = r["engagement"]["engagement_id"], r["submission_id"]
    did = client.post(f"/engagements/{eid}/documents:presign",
                      json={"filename": "SOW.pdf", "submission_id": sid}).json()["document_id"]

    # Parsing ran and found nothing it could identify, so no slot was ever filled.
    assert repo.list_submissions(eid)[0].docs() == {}
    assert not _all_documents_extracted(repo, eid), "not extracted yet"

    repo.put_document_version(DocumentVersion(
        engagement_id=eid, document_id=did, version=1, s3_key="k",
        status="extracted", uploaded_at=utcnow(),
    ))
    assert _all_documents_extracted(repo, eid), "the pipeline is done, so it must advance"

    repo.update_submission_status(
        eid, sid, SubmissionStatus.DRAFT.value, SubmissionStatus.EXTRACTING.value
    )
    repo.update_submission_status(
        eid, sid, SubmissionStatus.EXTRACTING.value, SubmissionStatus.IN_UNDERWRITING.value
    )
    assert client.get(f"/engagements/{eid}").json()["submission"]["status"] == "IN_UNDERWRITING"


def _doc_version(repo, eid, did, status):
    from app.store.models import DocumentVersion
    from app.store.repository import utcnow

    doc = repo.get_document(eid, did)
    repo.put_document_version(DocumentVersion(
        engagement_id=eid, document_id=did, version=doc.current_version,
        s3_key=f"{eid}/{did}/v{doc.current_version:04d}.pdf", status=status,
        uploaded_at=utcnow(),
    ))


def test_extraction_targets_only_documents_that_need_reading(ctx):  # noqa: F811
    """Re-reading an already-extracted agreement costs a model call and would discard the
    corrections an analyst made to its terms."""
    client, repo, state = ctx
    from app.validation.reconcile import pending_documents

    cid = _customer(client)
    r = client.post("/engagements", json={"name": "E", "customer_id": cid}).json()
    eid, sid = r["engagement"]["engagement_id"], r["submission_id"]

    done = client.post(f"/engagements/{eid}/documents:presign",
                       json={"doc_type": "MLA", "filename": "old.pdf", "submission_id": sid},
                       ).json()["document_id"]
    fresh = client.post(f"/engagements/{eid}/documents:presign",
                        json={"doc_type": "MSA", "filename": "new.pdf", "submission_id": sid},
                        ).json()["document_id"]
    _doc_version(repo, eid, done, "extracted")
    _doc_version(repo, eid, fresh, "uploaded")

    assert [d.document_id for d in pending_documents(repo, eid)] == [fresh]

    docs = {d["document_id"]: d for d in client.get(f"/engagements/{eid}").json()["documents"]}
    assert docs[done]["needs_extraction"] is False
    assert docs[fresh]["needs_extraction"] is True


def test_a_single_document_can_be_extracted_on_its_own(ctx):  # noqa: F811
    client, repo, state = ctx
    cid = _customer(client)
    r = client.post("/engagements", json={"name": "E", "customer_id": cid}).json()
    eid, sid = r["engagement"]["engagement_id"], r["submission_id"]
    did = client.post(f"/engagements/{eid}/documents:presign",
                      json={"doc_type": "MLA", "filename": "a.pdf", "submission_id": sid},
                      ).json()["document_id"]
    _doc_version(repo, eid, did, "uploaded")

    ok = client.post(f"/engagements/{eid}/documents/{did}:extract")
    assert ok.status_code == 200
    assert ok.json()["submission"]["status"] == "EXTRACTING"

    # Once read, asking again is refused rather than silently spending another call.
    _doc_version(repo, eid, did, "extracted")
    again = client.post(f"/engagements/{eid}/documents/{did}:extract")
    assert again.status_code == 409 and "already been extracted" in again.json()["detail"]
