"""Build the tool schema from the Pydantic contract so schema and tool never drift.

The tool is **byte-identical across all five extraction calls**. Tool definitions render at the
very start of a request, ahead of the system prompt and the document, so a tool that varied per
category would rebuild the prompt cache on every call and cost four fifths of the saving with no
visible symptom. Which record types a call may return is stated in the instruction instead.
"""

from __future__ import annotations

import copy
from typing import Any

from ..extraction.schema import ContractExtraction
from .base import Tool

_DESCRIPTION = (
    "Record what a fleet contract says, as a list of typed records: priced items, service "
    "level standards, reporting requirements, definitions, online tools, responsibilities, "
    "signatures and other sections. Copy the contract's own wording, give every record a "
    "verbatim quote, and never invent a value you cannot find."
)

# Fields the server owns. The model is told not to fill them, and they are removed from the
# schema so it is not even offered the chance: a hallucinated program_id would silently attach
# a term to the wrong catalog entry, and a hallucinated bbox would draw a highlight over the
# wrong part of the page.
_SERVER_ASSIGNED: frozenset[str] = frozenset(
    {
        "program_id",
        "item_id",
        "catalog_match",
        "program_ids",
        "signed_at",
        "bbox",
        "char_span",
        "engine",
        "party",
        "schema_version",
    }
)


def _prune(node: Any) -> Any:
    """Drop server-assigned properties everywhere they appear in the generated schema."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                value = {k: _prune(v) for k, v in value.items() if k not in _SERVER_ASSIGNED}
            elif key == "required" and isinstance(value, list):
                value = [r for r in value if r not in _SERVER_ASSIGNED]
            else:
                value = _prune(value)
            out[key] = value
        return out
    if isinstance(node, list):
        return [_prune(v) for v in node]
    return node


def build_contract_tool() -> Tool:
    schema = _prune(copy.deepcopy(ContractExtraction.model_json_schema()))
    return Tool(name="record_contract_terms", description=_DESCRIPTION, input_schema=schema)


CONTRACT_TOOL: Tool = build_contract_tool()
