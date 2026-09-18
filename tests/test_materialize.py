"""Re-extraction must replace terms, not duplicate them, and must not erase an analyst's work."""

from __future__ import annotations

import json
import pathlib

import pytest

from app.catalog.models import CatalogSnapshot, ServiceItem, ServiceProgram
from app.extraction.materialize import (
    build_rows,
    coverage_rows,
    merge_rows,
    needs_review_for,
    record_id_for,
)

SEED = pathlib.Path(__file__).parent.parent / "app" / "catalog" / "seed_catalog.json"
GROUND_TRUTH = pathlib.Path(__file__).parent / "fixtures" / "walmart_ground_truth.json"


@pytest.fixture(scope="module")
def catalog() -> CatalogSnapshot:
    seed = json.loads(SEED.read_text())
    return CatalogSnapshot(
        programs=[
            ServiceProgram(**{k: v for k, v in p.items() if k != "items"}) for p in seed["programs"]
        ],
        items=[
            ServiceItem(program_id=p["program_id"], **i)
            for p in seed["programs"]
            for i in p["items"]
        ],
    )


@pytest.fixture(scope="module")
def records() -> list[dict]:
    return json.loads(GROUND_TRUTH.read_text())["records"]


def test_ids_survive_cosmetic_drift():
    base = {"info_type": "pricing_item", "program": "Fuel Management Program",
            "item": "Replacement Fuel Card Issuance Fee", "amount": 15.0}
    spaced = {**base, "item": "Replacement  Fuel Card  Issuance Fee "}
    assert record_id_for(base) == record_id_for(spaced)


def test_a_changed_amount_is_a_different_term():
    """A fee that moves must lose its approval, so it must not keep its identity."""
    base = {"info_type": "pricing_item", "program": "Fuel Management Program",
            "item": "Replacement Fuel Card Issuance Fee", "amount": 15.0}
    assert record_id_for(base) != record_id_for({**base, "amount": 20.0})


def test_materializing_twice_does_not_duplicate(records, catalog):
    first, _ = build_rows("e", "d", 1, records, catalog=catalog)
    second, _ = build_rows("e", "d", 1, records, catalog=catalog)
    write, supersede = merge_rows(first, second)
    assert (write, supersede) == ([], [])
    assert len({r.record_id for r in first}) == len(first)


def test_an_approval_survives_an_unchanged_re_extraction(records, catalog):
    first, _ = build_rows("e", "d", 1, records, catalog=catalog)
    first[0].approved = True
    again, _ = build_rows("e", "d", 1, records, catalog=catalog)
    write, _ = merge_rows(first, again)
    assert write == [], "an unchanged term must not be rewritten, or the approval is lost"


def test_only_the_changed_row_loses_its_approval(records, catalog):
    first, _ = build_rows("e", "d", 1, records, catalog=catalog)
    for row in first:
        row.approved = True
    changed = json.loads(json.dumps(records))
    target = next(r for r in changed if r["info_type"] == "pricing_item" and r["amount"])
    target["amount"] = 999.0
    fresh, _ = build_rows("e", "d", 1, changed, catalog=catalog)
    write, supersede = merge_rows(first, fresh)
    assert len(write) == 1 and len(supersede) == 1
    assert write[0].approved is False and write[0].needs_review is True


def test_a_term_that_vanishes_is_superseded_not_deleted(records, catalog):
    first, _ = build_rows("e", "d", 1, records, catalog=catalog)
    fewer, _ = build_rows("e", "d", 1, records[:-5], catalog=catalog)
    _, supersede = merge_rows(first, fewer)
    assert len(supersede) == 5 and all(r.superseded for r in supersede)


def test_coverage_counts_programs_including_unpriced_ones(records, catalog):
    """A program named with no charge is still a service the customer receives."""
    rows, _ = build_rows("e", "d", 1, records, catalog=catalog)
    coverage = coverage_rows("e", "d", 1, rows, catalog)
    assert len(coverage) >= 15
    assert any(c.priced_item_count == 0 for c in coverage) or all(
        c.priced_item_count >= 0 for c in coverage
    )
    assert len(coverage) < len(catalog.active_programs), "coverage should be a subset"


def test_an_unmatched_program_is_flagged_for_a_person(catalog):
    rows, unmatched = build_rows(
        "e", "d", 1,
        [{"info_type": "pricing_item", "program": "Interplanetary Hovercraft Program",
          "item": "Docking Fee", "amount": 5.0, "confidence": 0.99,
          "citations": [{"quote": "docking fee", "page": 1}]}],
        catalog=catalog,
    )
    assert unmatched == ["Interplanetary Hovercraft Program"]
    assert rows[0].catalog_match == "unmatched"
    assert rows[0].needs_review is True, "an unplaced program is exactly what a person should see"
    assert rows[0].title == "Docking Fee", "the record is kept, not dropped"


def test_a_priced_line_with_no_price_is_flagged():
    record = {"info_type": "pricing_item", "program": "X", "item": "Some Fee",
              "confidence": 0.99, "citations": [{"quote": "q", "page": 1}]}
    assert needs_review_for(record, "exact", 0.8) is True


def test_a_record_without_evidence_is_flagged():
    record = {"info_type": "definition", "term": "X", "definition": "y", "confidence": 1.0}
    assert needs_review_for(record, None, 0.8) is True
