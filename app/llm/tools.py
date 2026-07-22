"""Build the tool schema from the Pydantic contract so schema and tool never drift."""

from __future__ import annotations

from ..extraction.schema import ContractExtraction
from .base import Tool

_DESCRIPTION = (
    "Record every billing-relevant term extracted from a fleet MSA or MLA contract. "
    "Populate all 13 service lines (mark elected=false for services the contract does not "
    "elect), express each fee with the correct fee_type, and include a verbatim `quote` in "
    "every citation. Never invent values you cannot find; lower `confidence` when uncertain."
)


def build_contract_tool() -> Tool:
    schema = ContractExtraction.model_json_schema()
    return Tool(name="record_contract_terms", description=_DESCRIPTION, input_schema=schema)


CONTRACT_TOOL: Tool = build_contract_tool()
