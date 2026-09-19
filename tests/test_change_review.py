"""Change verification: what the revised agreement did against what the customer asked for.

Two things went wrong in dev and are pinned here. A revision uploaded as its own file rather
than as a replacement left two documents at version 1, and the comparison — which only ever
looked at version N against N-1 of one document — decided there was nothing to verify. And the
comparison keyed on the model's own wording, so re-reading the very same PDF produced a screen
of changes nobody had made.
"""

from __future__ import annotations

from app.lifecycle.submission_state import SubmissionStatus
from app.review.insights import _changes_and_versions, _predecessor, _priced_terms
from app.store.models import Document, DocumentStanding, Submission, TermRow
from app.store.repository import Repository, utcnow

from .test_api import ctx  # noqa: F401 - the shared moto fixture


def _doc(repo: Repository, eid: str, did: str, *, version=1, standing="CURRENT", created="2026-01-01"):
    return repo.put_document(Document(
        engagement_id=eid, document_id=did, doc_type="MSA", filename=f"{did}.pdf",
        current_version=version, standing=standing, cycle=1, created_at=created,
    ))


def _term(repo: Repository, eid: str, did: str, version: int, program: str, amount, **rec):
    record = {"program": program, "item": "Monthly Program Fee", "amount": amount,
              "frequency": "per vehicle per month", **rec}
    repo.put_terms([TermRow(
        engagement_id=eid, document_id=did, version=version,
        record_id=f"{did}-{version}-{program}-{rec.get('item', 'fee')}",
        category="pricing", info_type="pricing_item", title="Monthly Program Fee",
        subtitle=program, amount=amount, frequency="per vehicle per month",
        unit_basis="per_vehicle_per_month", record=record, created_at=utcnow(),
    )])


def _submission(repo: Repository, eid: str, did: str) -> Submission:
    return repo.put_submission(Submission(
        engagement_id=eid, submission_id="s1", status=SubmissionStatus.IN_UNDERWRITING,
        cycle=1, round=2, document_ids={"MSA": did}, created_at=utcnow(),
        updated_at=utcnow(),
    ))


def test_a_revision_uploaded_as_its_own_file_is_still_a_revision(ctx):  # noqa: F811
    """The dev case: two documents, both version 1, one superseding the other."""
    client, repo, state = ctx
    eid = "e1"
    old = _doc(repo, eid, "old", standing=DocumentStanding.SUPERSEDED.value, created="2026-01-01")
    new = _doc(repo, eid, "new", created="2026-02-01")
    _term(repo, eid, "old", 1, "Maintenance Management Program", 12.5)
    _term(repo, eid, "new", 1, "Maintenance Management Program", 10.5)

    assert _predecessor(repo, eid, new) == ("old", 1)
    # And the other direction is not a predecessor of itself.
    assert _predecessor(repo, eid, old) is None

    changes, sig = _changes_and_versions(repo, _submission(repo, eid, "new"))
    assert "<" in sig, "the signature must name both sides, or nothing is verifiable"
    assert [c["service"] for c in changes] == ["Maintenance Management Program"]
    assert "12.5" in changes[0]["before"] and "10.5" in changes[0]["after"]


def test_a_replacement_compares_against_its_own_previous_version(ctx):  # noqa: F811
    client, repo, state = ctx
    eid = "e2"
    doc = _doc(repo, eid, "d", version=2)
    _term(repo, eid, "d", 1, "Fuel Management Program", 3.25)
    _term(repo, eid, "d", 2, "Fuel Management Program", 3.0)

    assert _predecessor(repo, eid, doc) == ("d", 1)
    changes, _ = _changes_and_versions(repo, _submission(repo, eid, "d"))
    # Amounts arrive back from the store as Decimals, so compare the reading, not its spelling.
    assert len(changes) == 1
    assert "3.25" in changes[0]["before"] and changes[0]["after"] != changes[0]["before"]


def test_a_first_upload_has_nothing_to_verify(ctx):  # noqa: F811
    client, repo, state = ctx
    eid = "e3"
    doc = _doc(repo, eid, "only")
    _term(repo, eid, "only", 1, "Toll Management Program", 1.75)
    assert _predecessor(repo, eid, doc) is None
    changes, sig = _changes_and_versions(repo, _submission(repo, eid, "only"))
    assert changes == [] and "<" not in sig


def test_wording_that_moved_is_not_a_change_in_the_money(ctx):  # noqa: F811
    """Both sides are readings. Two reads of one page must not disagree about the price."""
    client, repo, state = ctx
    eid = "e4"
    _doc(repo, eid, "before", standing=DocumentStanding.SUPERSEDED.value, created="2026-01-01")
    _doc(repo, eid, "after", created="2026-02-01")
    # Same money, read with a different label, a plural, and the contract's other phrasing.
    _term(repo, eid, "before", 1, "Fuel Management Program", 3.25, item="ISP Charges")
    _term(repo, eid, "after", 1, "Fuel Management  Program!", 3.25, item="ISP Charge")

    changes, _ = _changes_and_versions(repo, _submission(repo, eid, "after"))
    assert changes == [], f"wording alone produced a change: {changes}"


def test_a_row_carrying_no_money_cannot_be_a_pricing_change(ctx):  # noqa: F811
    """A unit basis on its own says how it would be charged, not that a price moved."""
    client, repo, state = ctx
    eid = "e5"
    _doc(repo, eid, "b", standing=DocumentStanding.SUPERSEDED.value, created="2026-01-01")
    _doc(repo, eid, "a", created="2026-02-01")
    _term(repo, eid, "b", 1, "Online Tools", None, item="Releases")
    _term(repo, eid, "a", 1, "Online Tools", None, item="Release")
    assert _priced_terms(repo.list_terms(eid, "b", 1, "pricing")) == {}
    assert _changes_and_versions(repo, _submission(repo, eid, "a"))[0] == []


def test_an_upload_that_never_arrived_can_be_taken_back(ctx):  # noqa: F811
    """A presigned row exists before the bytes do; a failed PUT must not leave an agreement."""
    client, repo, state = ctx
    r = client.post("/customers", json={"legal_name": "Apex Pvt Ltd"})
    cid = r.json()["customer"]["customer_id"]
    eid = client.post(
        "/engagements", json={"name": "E", "customer_id": cid}
    ).json()["engagement"]["engagement_id"]
    sub = repo.current_submission(eid)

    first = client.post(f"/engagements/{eid}/documents:presign",
                        json={"filename": "a.pdf", "submission_id": sub.submission_id,
                              "doc_type": "MSA"}).json()
    did = first["document_id"]
    second = client.post(f"/engagements/{eid}/documents:presign",
                         json={"filename": "b.pdf", "submission_id": sub.submission_id,
                               "document_id": did}).json()
    assert second["version"] == 2 and repo.get_document(eid, did).current_version == 2

    # Version 2's file never arrived: the document goes back to version 1, and stays.
    assert client.delete(f"/engagements/{eid}/documents/{did}/versions/2").status_code == 200
    assert repo.get_document(eid, did).current_version == 1
    assert repo.get_document_version(eid, did, 2) is None

    # A version that has been read is part of the record and is not taken back this way.
    version = repo.get_document_version(eid, did, 1)
    version.status = "extracted"
    repo.put_document_version(version)
    assert client.delete(f"/engagements/{eid}/documents/{did}/versions/1").status_code == 409

    # And a first version that never arrived takes the whole document with it.
    version.status = "uploaded"
    repo.put_document_version(version)
    assert client.delete(f"/engagements/{eid}/documents/{did}/versions/1").status_code == 200
    assert repo.get_document(eid, did) is None
