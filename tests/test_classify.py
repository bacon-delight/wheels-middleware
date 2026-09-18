"""Reading an agreement's type, date and standing off its own text."""

from __future__ import annotations

import pytest

from app.validation.classify import (
    classify_doc_type,
    derive_scope,
    find_effective_date,
    rank_standing,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("WHEELS INC. MASTER LEASE AGREEMENT ... master lease agreement", "MLA"),
        ("MASTER SERVICE AGREEMENT ... this master services agreement", "MSA"),
        ("a delivery note with no agreement title", "UNKNOWN"),
        ("", "UNKNOWN"),
    ],
)
def test_classify_reads_the_title(text, expected):
    assert classify_doc_type(text)[0] == expected


def test_a_lease_that_cites_the_service_agreement_is_still_a_lease():
    """An MLA routinely cross-references the MSA, so presence alone cannot decide it."""
    text = (
        "MASTER LEASE AGREEMENT. This master lease agreement governs. Where a master service "
        "agreement exists, registration is administered thereunder. master lease agreement."
    )
    doc_type, confidence = classify_doc_type(text)
    assert doc_type == "MLA" and confidence > 0.6


def test_an_even_split_falls_back_to_whichever_opens_the_document():
    text = "MASTER SERVICE AGREEMENT" + " filler " * 200 + "master lease agreement"
    assert classify_doc_type(text)[0] == "MSA"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("effective as of January 15, 2024 by and between", "2024-01-15"),
        ("dated as of 3 March 2022", "2022-03-03"),
        ("Effective Date: 2021-07-01", "2021-07-01"),
        ("Dated the 1st day of no month at all", None),
        ("", None),
    ],
)
def test_effective_date(text, expected):
    assert find_effective_date(text) == expected


def test_the_newest_agreement_of_a_type_is_the_one_in_force():
    docs = [
        {"document_id": "old", "doc_type": "MLA", "effective_date": "2021-01-01", "created_at": "1"},
        {"document_id": "new", "doc_type": "MLA", "effective_date": "2025-01-01", "created_at": "2"},
        {"document_id": "svc", "doc_type": "MSA", "effective_date": None, "created_at": "3"},
    ]
    assert rank_standing(docs) == {"new": "CURRENT", "old": "SUPERSEDED", "svc": "CURRENT"}


def test_undated_agreements_fall_back_to_upload_order():
    docs = [
        {"document_id": "first", "doc_type": "MSA", "effective_date": None, "created_at": "2024-01-01"},
        {"document_id": "second", "doc_type": "MSA", "effective_date": None, "created_at": "2024-06-01"},
    ]
    assert rank_standing(docs) == {"second": "CURRENT", "first": "SUPERSEDED"}


def test_a_dated_agreement_outranks_an_undated_one():
    docs = [
        {"document_id": "dated", "doc_type": "MSA", "effective_date": "2020-01-01", "created_at": "1"},
        {"document_id": "undated", "doc_type": "MSA", "effective_date": None, "created_at": "9"},
    ]
    assert rank_standing(docs)["dated"] == "CURRENT"


@pytest.mark.parametrize(
    "types,expected",
    [
        ({"MLA", "MSA"}, "LEASE_AND_SERVICE"),
        ({"MLA"}, "LEASE_ONLY"),
        ({"MSA"}, "SERVICE_ONLY"),
        (set(), None),
    ],
)
def test_scope_follows_the_agreements_in_force(types, expected):
    assert derive_scope(types) == expected
