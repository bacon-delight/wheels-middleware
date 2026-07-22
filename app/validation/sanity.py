"""Cheap, text-based sanity checks run on a re-uploaded document (no LLM).

Guards against the two common re-upload mistakes: the wrong client's contract, or the wrong
document type in a slot. A deeper field-level diff against the prior version happens during
extraction; this is the fast gate before we spend an extraction call.
"""

from __future__ import annotations

import re

_ENTITY_STOPWORDS = {"llc", "inc", "corp", "corporation", "company", "co", "ltd", "the"}


def sanity_check_text(
    full_text: str, client_name: str, expected_doc_type: str
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    low = full_text.lower()

    tokens = [
        t
        for t in re.split(r"\W+", client_name.lower())
        if len(t) > 3 and t not in _ENTITY_STOPWORDS
    ]
    if tokens and not any(t in low for t in tokens):
        reasons.append(f"document does not mention the client '{client_name}'")

    has_msa = "master service agreement" in low
    has_mla = "master lease agreement" in low
    if expected_doc_type == "MSA" and has_mla and not has_msa:
        reasons.append("expected an MSA but the document reads as a Master Lease Agreement")
    if expected_doc_type == "MLA" and has_msa and not has_mla:
        reasons.append("expected an MLA but the document reads as a Master Service Agreement")

    return (len(reasons) == 0, reasons)
