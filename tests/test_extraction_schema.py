"""The extraction schema must be able to hold a real contract as a person read it.

The strongest available check is the workbook in `tests/fixtures/walmart_ground_truth.json`:
158 records across all nine record types, transcribed by hand from a 41-page statement of work.
If the schema cannot express what a person found, no amount of prompt work will save it.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from pydantic import BaseModel

from app.extraction import schema as S

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "walmart_ground_truth.json"


@pytest.fixture(scope="module")
def ground_truth() -> dict:
    return json.loads(FIXTURE.read_text())


def test_every_hand_extracted_record_validates(ground_truth):
    """The completeness proof: all 158, not most of them."""
    good, bad = S.parse_records(ground_truth["records"])
    assert bad == [], f"{len(bad)} records the schema cannot hold: {bad[:3]}"
    assert len(good) == len(ground_truth["records"]) == 158


def test_all_nine_record_types_are_exercised(ground_truth):
    good, _ = S.parse_records(ground_truth["records"])
    assert {r.info_type for r in good} == set(S.RECORD_MODELS)


def test_the_document_round_trips(ground_truth):
    good, _ = S.parse_records(ground_truth["records"])
    extraction = S.ContractExtraction(
        doc_meta=S.DocumentMeta(**ground_truth["doc_meta"]), records=good
    )
    dumped = extraction.model_dump(mode="json")
    assert S.ContractExtraction.model_validate(dumped).model_dump(mode="json") == dumped


def test_categories_add_up(ground_truth):
    good, _ = S.parse_records(ground_truth["records"])
    counts = S.ContractExtraction(records=good).counts_by_category()
    assert counts == {"pricing": 34, "sla": 12, "reporting": 22, "misc": 90}


def test_a_program_named_without_a_price_is_still_a_record(ground_truth):
    """Twelve of the workbook's pricing rows name a program and price nothing under it.

    That is how coverage is answerable at all: it is the contract saying the customer is
    enrolled. A schema that required an item would drop every one of them.
    """
    good, _ = S.parse_records(ground_truth["records"])
    program_only = [r for r in good if r.info_type == "pricing_item" and r.item is None]
    assert len(program_only) == 12
    assert all(r.program for r in program_only)


def test_no_enum_field_can_fail_a_whole_document():
    """The regression guard for the failure this schema replaced.

    A single unrecognised value used to fail `model_validate` for the entire contract. Every
    enum-typed field must now either coerce unknown input or be a Literal the server sets.
    """
    offenders = []
    for name, model in vars(S).items():
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            continue
        validators = set()
        for decorator in model.__pydantic_decorators__.field_validators.values():
            if decorator.info.mode == "before":
                validators.update(decorator.info.fields)
        for field_name, field in model.model_fields.items():
            annotation = str(field.annotation)
            is_enum = any(f"{e.__name__}" in annotation for e in (
                S.FeeType, S.UnitBasis, S.ConditionType, S.EscalatorType, S.Party
            ))
            if is_enum and field_name not in validators:
                offenders.append(f"{name}.{field_name}")
    assert offenders == [], f"enum fields with no coercion: {offenders}"


def test_one_bad_record_does_not_lose_the_others():
    good, bad = S.parse_records(
        [
            {"info_type": "definition", "term": "A", "definition": "one"},
            {"info_type": "definition"},  # missing required fields
            {"info_type": "not_a_type"},
            {"info_type": "definition", "term": "B", "definition": "two"},
        ]
    )
    assert [r.term for r in good] == ["A", "B"]
    assert len(bad) == 2


def test_records_sent_as_a_json_string_are_recovered():
    """Models sometimes serialise the array as a string; that must not read as an empty document."""
    payload = json.dumps([{"info_type": "definition", "term": "A", "definition": "one"}])
    good, bad = S.parse_records(payload)
    assert len(good) == 1 and bad == []


def test_unescaped_quotes_inside_a_string_payload_are_repaired():
    """Contracts quote themselves constantly, and those quotes arrive unescaped."""
    payload = (
        '[{"info_type": "definition", "term": "SOW", '
        '"definition": "This Statement of Work ("SOW") as of the date below."}]'
    )
    good, bad = S.parse_records(payload)
    assert len(good) == 1 and bad == []
    assert '"SOW"' in good[0].definition


def test_a_truncated_payload_keeps_its_complete_records():
    """A response cut off at its token limit still yields everything before the cut."""
    payload = (
        '[{"info_type": "definition", "term": "A", "definition": "one"},'
        ' {"info_type": "definition", "term": "B", "definition": "two"},'
        ' {"info_type": "defini'
    )
    good, _ = S.parse_records(payload)
    assert [r.term for r in good] == ["A", "B"]


def test_a_number_where_prose_is_expected_is_kept_not_rejected():
    """The workbook writes a fee-credit multiplier as 0.25; a model will do the same."""
    good, bad = S.parse_records(
        [{"info_type": "sla_item", "category": "Initial Fee Credit", "calculation": 0.25}]
    )
    assert bad == [] and good[0].calculation == "0.25"


def test_a_citation_without_a_page_is_accepted():
    """The resolver is authoritative on the page, so requiring one would discard good evidence."""
    good, bad = S.parse_records(
        [{"info_type": "definition", "term": "A", "definition": "b",
          "citations": [{"quote": "a verbatim fragment"}]}]
    )
    assert bad == [] and good[0].citations[0].page is None
