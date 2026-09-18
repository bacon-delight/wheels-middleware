"""Live extraction scored against a human's own extraction of the same contract.

Skipped unless `--run-llm`, because it makes real model calls. What it reports is the number
this whole rework should be steered by: how much of what a person found do we also find.

    make test-llm
"""

from __future__ import annotations

import json
import pathlib

import pytest

pytestmark = pytest.mark.llm

CONTRACT = pathlib.Path(
    "../Documents/Actual Contracts/Walmart-Inc-SOW-4-4-25-w-Amend.pdf"
)
GROUND_TRUTH = pathlib.Path(__file__).parent / "fixtures" / "walmart_ground_truth.json"


@pytest.fixture(scope="module")
def run():
    if not CONTRACT.exists():
        pytest.skip(f"contract not available at {CONTRACT}")
    from app.extraction.service import extract_contract

    return extract_contract(str(CONTRACT), doc_type_hint="SOW")


def test_every_call_comes_back_whole(run):
    """Truncation is this system's most dangerous failure: it looks like finding less."""
    telemetry = run.telemetry()
    print(json.dumps({k: v for k, v in telemetry.items() if k != "calls"}, indent=2))
    assert run.truncated_calls == [], f"calls cut off at their limit: {run.truncated_calls}"
    assert run.failed_calls == [], f"calls that errored: {run.failed_calls}"


def test_the_prompt_cache_is_actually_working(run):
    """A cache that silently stops working costs money and changes nothing else."""
    reads = [c for c in run.calls if (c.cache_read_tokens or 0) > 0]
    assert len(reads) >= 3, "the document should be read from cache by every call after the first"


def test_citations_resolve_to_the_page(run):
    resolved = run.total_citations - run.unresolved_citations
    assert run.total_citations > 0
    assert resolved / run.total_citations >= 0.95


def test_recall_against_the_hand_extraction(run):
    """The headline: the workbook is the floor, not the ceiling."""
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))
    from score_extraction import _by_type, score_type  # noqa: PLC0415

    expected = _by_type(json.loads(GROUND_TRUTH.read_text())["records"])
    ours = run.extraction.model_dump(mode="json")["records"]
    got = _by_type(ours)

    # Pricing carries the money, so it is held to the highest bar.
    pricing = score_type("pricing_item", expected["pricing_item"], got.get("pricing_item", []), ours)
    print(f"pricing recall {pricing['recall']:.0%} ({pricing['found']}/{pricing['expected']})")
    assert pricing["recall"] >= 0.9, f"missed pricing rows: {pricing['missed'][:5]}"

    want_programs = {r["program"] for r in expected["pricing_item"] if r.get("program")}
    got_programs = {r.get("program") for r in got.get("pricing_item", [])}
    from app.catalog.resolve import normalise

    missing = {p for p in want_programs if normalise(p) not in {normalise(g) for g in got_programs}}
    assert missing == set(), f"programs the contract names that we missed: {missing}"

    covered = sum(
        score_type(t, rows, got.get(t, []), ours)["found"]
        + len(score_type(t, rows, got.get(t, []), ours)["elsewhere"])
        for t, rows in expected.items()
    )
    total = sum(len(v) for v in expected.values())
    print(f"overall coverage {covered / total:.0%} ({covered}/{total})")
    assert covered / total >= 0.8
