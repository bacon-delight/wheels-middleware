"""Shared fixtures and the --run-llm gate.

Deterministic tests (schema, engine, citations) always run and cost nothing. Tests marked
`@pytest.mark.llm` make a live Bedrock/Anthropic call and are skipped unless --run-llm is
passed, keeping CI green and $0.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ocr.pymupdf_engine import PyMuPDFEngine

# Bundled in the repo so the suite is self-contained in CI (the source Documents/ folder
# lives outside the repo).
DOCS_DIR = Path(__file__).resolve().parent / "fixtures"
SAMPLE_PDFS = (
    "MSA_MeridianFoods.pdf",
    "MLA_MeridianFoods.pdf",
    "MSA_ApexFieldServices.pdf",
    "MLA_ApexFieldServices.pdf",
)


def pytest_addoption(parser):
    parser.addoption(
        "--run-llm",
        action="store_true",
        default=False,
        help="Run tests that make a live LLM call (Bedrock/Anthropic).",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-llm"):
        return
    skip = pytest.mark.skip(reason="needs --run-llm (live LLM call)")
    for item in items:
        if "llm" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def docs_dir() -> Path:
    return DOCS_DIR


@pytest.fixture(scope="session")
def engine() -> PyMuPDFEngine:
    return PyMuPDFEngine()


@pytest.fixture(scope="session")
def parsed_docs(engine):
    """filename -> DocumentParse for all four sample PDFs (parsed once)."""
    return {name: engine.parse(DOCS_DIR / name) for name in SAMPLE_PDFS}
