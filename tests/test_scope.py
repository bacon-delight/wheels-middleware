"""Engagement scope decides which master agreements apply.

Lease -> MLA, service -> MSA, both -> both. Table-driven in the style of test_state_machine.
"""

from __future__ import annotations

import pytest

from app.store.models import EngagementScope, Submission, required_doc_types
from app.store.repository import utcnow


@pytest.mark.parametrize(
    "scope,expected",
    [
        (EngagementScope.LEASE_ONLY.value, ("MLA",)),
        (EngagementScope.SERVICE_ONLY.value, ("MSA",)),
        (EngagementScope.LEASE_AND_SERVICE.value, ("MLA", "MSA")),
    ],
)
def test_required_docs_per_scope(scope, expected):
    assert required_doc_types(scope) == expected


def test_unknown_scope_falls_back_to_both():
    """An un-backfilled row must keep behaving exactly as it did before scope existed."""
    assert required_doc_types(None) == ("MLA", "MSA")
    assert required_doc_types("NONSENSE") == ("MLA", "MSA")


def _sub(**kw) -> Submission:
    return Submission(
        engagement_id="e1", submission_id="s1", created_at=utcnow(), updated_at=utcnow(), **kw
    )


def test_docs_reads_the_slot_map():
    assert _sub(document_ids={"MLA": "d1"}).docs() == {"MLA": "d1"}


def test_docs_hydrates_from_legacy_scalar_fields():
    """Rows written before the slot map existed must still resolve their documents."""
    sub = _sub(msa_document_id="m1", mla_document_id="l1")
    assert sub.docs() == {"MSA": "m1", "MLA": "l1"}


def test_docs_ignores_empty_legacy_slots():
    assert _sub(mla_document_id="l1").docs() == {"MLA": "l1"}


def test_with_doc_keeps_legacy_fields_in_sync():
    """Both representations are written so a half-deployed reader cannot see a stale slot."""
    patch = _sub(mla_document_id="l1").with_doc("MSA", "m2")
    assert patch["document_ids"] == {"MLA": "l1", "MSA": "m2"}
    assert patch["msa_document_id"] == "m2"


def test_with_doc_replaces_an_existing_slot():
    patch = _sub(document_ids={"MSA": "old"}).with_doc("MSA", "new")
    assert patch["document_ids"] == {"MSA": "new"} and patch["msa_document_id"] == "new"
