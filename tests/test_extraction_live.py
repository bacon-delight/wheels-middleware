"""Live extraction eval (gated by --run-llm).

Runs the real extractor (Bedrock or Anthropic) against each sample PDF and asserts the
section-4 expectations pass. Also prints token usage so we can confirm per-doc cost before
running the full UAT batch (the cost gate in the plan).

Run with:  pytest -m llm --run-llm -s
Provider:  LLM_PROVIDER=bedrock (default) or LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=...
"""

from __future__ import annotations

import pytest

from app.extraction.service import extract_contract
from tests.golden import ground_truth as gt

pytestmark = pytest.mark.llm


@pytest.mark.parametrize("filename", gt.ALL_DOCS)
def test_live_extraction_matches_golden(docs_dir, filename):
    doc_type_hint = "MLA" if filename.startswith("MLA") else "MSA"
    result = extract_contract(docs_dir / filename, doc_type_hint=doc_type_hint)

    print(
        f"\n[{filename}] model={result.model} "
        f"in={result.input_tokens} out={result.output_tokens} "
        f"unresolved_citations={result.unresolved_citations} "
        f"needs_review={len(result.needs_review)}"
    )

    failures = gt.check(result.extraction, filename)
    assert not failures, f"{filename} live extraction missed: {failures}"


@pytest.mark.parametrize("filename", ("MLA_ApexFieldServices.pdf", "MLA_MeridianFoods.pdf"))
def test_live_extraction_citations_resolve_to_pages(docs_dir, filename):
    """Every citation the model returns must be locatable in the document geometry."""
    result = extract_contract(docs_dir / filename, doc_type_hint="MLA")
    assert result.unresolved_citations == 0, (
        f"{filename}: {result.unresolved_citations} citations did not resolve to a bbox"
    )
