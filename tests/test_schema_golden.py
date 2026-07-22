"""The golden dataset proves the schema expresses every pricing shape, and each golden
object satisfies its own section-4 expectations (self-consistency, no LLM)."""

from __future__ import annotations

import pytest

from app.extraction.schema import ContractExtraction, ServiceLine
from tests.golden import ground_truth as gt


@pytest.mark.parametrize("filename", gt.ALL_DOCS)
def test_golden_builds_and_roundtrips(filename):
    ex = gt.golden_extraction(filename)
    assert isinstance(ex, ContractExtraction)
    # Round-trips through JSON without loss (this is what we store / send over the API).
    dumped = ex.model_dump(mode="json")
    reloaded = ContractExtraction.model_validate(dumped)
    assert reloaded.model_dump(mode="json") == dumped


@pytest.mark.parametrize("filename", gt.ALL_DOCS)
def test_golden_satisfies_its_own_expectations(filename):
    ex = gt.golden_extraction(filename)
    failures = gt.check(ex, filename)
    assert not failures, f"{filename} golden fails its own expectations: {failures}"


def test_every_msa_lists_all_thirteen_service_lines():
    for filename in ("MSA_MeridianFoods.pdf", "MSA_ApexFieldServices.pdf"):
        ex = gt.golden_extraction(filename)
        services = {sl.service for sl in ex.service_lines}
        assert services == {s.value for s in ServiceLine}


def test_apex_rentals_not_elected_but_present():
    """The trap: Rentals must appear as elected=false, never silently dropped."""
    ex = gt.golden_extraction("MSA_ApexFieldServices.pdf")
    rentals = ex.line(ServiceLine.RENTALS)
    assert rentals is not None and rentals.elected is False


def test_schema_expresses_tiers_conditions_costplus():
    ex = gt.golden_extraction("MSA_ApexFieldServices.pdf")
    maint = ex.line(ServiceLine.MAINTENANCE)
    assert any(fi.tier_bands for fi in maint.fee_items), "tier bands not expressible"
    coll = ex.line(ServiceLine.COLLISION)
    assert any(
        c.trigger == "telematics_enrolled" for fi in coll.fee_items for c in fi.conditions
    ), "conditional waiver not expressible"
    assert any(fi.fee_type == "cost_plus" for fi in coll.fee_items), "cost-plus not expressible"
