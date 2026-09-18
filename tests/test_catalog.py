"""The catalog is the denominator. If it is wrong, every coverage number is wrong."""

from __future__ import annotations

import json
import pathlib

import pytest

from app.catalog.models import CatalogSnapshot, ServiceItem, ServiceProgram
from app.catalog.resolve import normalise, resolve, resolve_program, slugify

SEED = pathlib.Path(__file__).parent.parent / "app" / "catalog" / "seed_catalog.json"
GROUND_TRUTH = pathlib.Path(__file__).parent / "fixtures" / "walmart_ground_truth.json"


@pytest.fixture(scope="module")
def catalog() -> CatalogSnapshot:
    seed = json.loads(SEED.read_text())
    return CatalogSnapshot(
        programs=[
            ServiceProgram(**{k: v for k, v in p.items() if k != "items"})
            for p in seed["programs"]
        ],
        items=[
            ServiceItem(program_id=p["program_id"], **i)
            for p in seed["programs"]
            for i in p["items"]
        ],
    )


def test_the_corpus_wordings_collapse_onto_one_key():
    """Every way the corpus writes the fuel programme must normalise identically."""
    variants = [
        "Fuel Management Program",
        "Fuel Management",
        "FUEL MANAGEMENT PROGRAM (SECTION 2(B) OF THIS SOW)",
        "Fuel Management Services",
    ]
    assert len({normalise(v) for v in variants}) == 1


def test_slugs_are_stable_and_readable():
    assert slugify("Motor Vehicle Record (MVR) Program") == "motor-vehicle-record"
    assert slugify("Fuel Management Program") == slugify("Fuel Management")


def test_every_walmart_program_resolves(catalog):
    """The workbook names 19 programs. Coverage is meaningless if the catalog misses any."""
    names = {
        r["program"]
        for r in json.loads(GROUND_TRUTH.read_text())["records"]
        if r["info_type"] == "pricing_item" and r.get("program")
    }
    unresolved = [n for n in names if resolve_program(catalog, n).program is None]
    assert unresolved == [], f"catalog cannot place: {unresolved}"


def test_resolution_reports_how_it_matched(catalog):
    exact = resolve_program(catalog, "Fuel Management Program")
    assert exact.kind == "exact" and exact.program_id == "fuel-management"
    loose = resolve_program(catalog, "Fuel Management")
    assert loose.kind in ("alias", "normalised") and loose.program_id == "fuel-management"


def test_an_unknown_program_stays_unknown(catalog):
    """Never guessed onto a neighbour: a wrong program is worse than an unmatched one."""
    found = resolve_program(catalog, "Interplanetary Hovercraft Program")
    assert found.program is None and found.kind == "unmatched"


def test_two_equally_close_candidates_refuse_to_match():
    """A fuzzy match needs a clear winner, or it reports nothing."""
    snapshot = CatalogSnapshot(
        programs=[
            ServiceProgram(program_id="alpha-management", name="Alpha Management Program"),
            ServiceProgram(program_id="alphb-management", name="Alphb Management Program"),
        ]
    )
    assert resolve_program(snapshot, "Alphc Management Program").program is None


def test_an_item_never_resolves_across_programs(catalog):
    """`Monthly Program Fee` exists under twenty-two programs; identity is the pair."""
    fuel = resolve(catalog, "Fuel Management Program", "Replacement Fuel Card Issuance Fee")
    assert fuel.program_id == "fuel-management"
    collision = resolve(catalog, "Collision Management Program", "Replacement Fuel Card Issuance Fee")
    assert collision.program_id == "collision-management"
    assert collision.item is None or collision.item.program_id == "collision-management"


def test_the_catalog_is_big_enough_to_be_a_denominator(catalog):
    assert len(catalog.active_programs) >= 30
    assert len(catalog.items) >= 100
