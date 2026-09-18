"""Convert a hand-made contract extraction workbook into committed ground truth.

The workbook at `Documents/Walmart Contract Extraction.xlsx` is a person's extraction of the
Walmart SOW: 158 records across nine record types. It is the target this system is being built
to reach, so it belongs in the repo as a fixture rather than in a folder outside it — CI cannot
see `Documents/`, and the recall score is the metric the whole rework is steered by.

Run once, review the diff, commit the output:

    python scripts/import_ground_truth.py \
        "../Documents/Walmart Contract Extraction.xlsx" \
        tests/fixtures/walmart_ground_truth.json

The conversion is deliberately dumb. It renames columns and nothing else: no inference, no
cleanup, no dropping of rows that look empty. A fixture that has been tidied is no longer
ground truth, and every difference between it and our output should be our difference.
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys

try:
    import openpyxl
except ImportError:  # pragma: no cover - a local authoring tool, not a runtime dependency
    sys.exit("openpyxl is required: pip install openpyxl")

# Sheet name -> (info_type, {workbook column: record field}).
# The three columns every sheet shares (Info Type, Contract Section, Context, Notes) are
# handled separately in _common, so they are absent here.
SHEETS: dict[str, tuple[str, dict[str, str]]] = {
    "Pricing Items": (
        "pricing_item",
        {
            "Program": "program",
            "Product / Service": "product_service",
            "Item": "item",
            "SubCategory": "sub_category",
            "Frequency": "frequency",
            "Amount": "amount",
            "Calculation": "calculation",
        },
    ),
    "SLA Items": (
        "sla_item",
        {
            "Contract Sub-Section": "contract_sub_section",
            "Category": "category",
            "Service Level Standard": "service_level_standard",
            "Frequency*": "frequency",
            "Minimum Threshold": "minimum_threshold",
            "Calculation": "calculation",
            "Example": "example",
        },
    ),
    "Reporting Requirements": (
        "reporting_requirement",
        {
            "Report Name": "report_name",
            "Report Specifications": "report_specifications",
            "Frequency": "frequency",
            "Applicable Program": "applicable_programs",
        },
    ),
    "Misc - Definitions": (
        "definition",
        {"Term": "term", "Definition": "definition"},
    ),
    "Misc - Online Tools": (
        "online_tool",
        {"Platform": "platform", "Online Tool": "tool_name", "Description": "description"},
    ),
    "Misc - Responsibilities": (
        "responsibility",
        {
            "Topic": "topic",
            "Task": "task",
            "Responsible Party": "responsible_party",
            "Timing": "timing",
            "Frequency": "frequency",
        },
    ),
    "Misc - Signatures": (
        "signature",
        {"Company": "company", "Date": "signed_date_raw", "Name": "name", "Title": "title"},
    ),
    "Misc - Information Section": (
        "information_section",
        {"Topic": "topic", "Description": "description"},
    ),
    "Misc - TBD": (
        "uncategorised",
        {"Item": "item", "TBD": "detail"},
    ),
}

# The workbook newline-joins several programs into one cell on the reporting sheet.
_LIST_FIELDS = {"applicable_programs"}

HEADER_ROW = 6  # rows 1-3 are document metadata, rows 4-5 are blank


def _clean(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return value


def _doc_meta(ws) -> dict[str, object]:
    """Rows 1-3 of every sheet carry the same three document facts."""
    out: dict[str, object] = {}
    labels = {
        "Client Name": "client_name",
        "Document Name": "document_name",
        "Document Effective Date": "effective_date",
    }
    for row in ws.iter_rows(min_row=1, max_row=4, values_only=True):
        if not row or row[0] is None:
            continue
        field = labels.get(str(row[0]).strip())
        if field:
            out[field] = _clean(row[1] if len(row) > 1 else None)
    return out


def _common(row: dict[str, object]) -> dict[str, object]:
    """The columns every sheet shares.

    The workbook's "Context (Contract snipet)" is the verbatim quote our own extraction puts in
    `citations[0].quote`, so it is imported as a citation with no page. We do not know the page
    the person was looking at, and inventing one would make the fixture lie about evidence.
    """
    quote = row.get("Context (Contract snipet)")
    return {
        "contract_section": row.get("Contract Section"),
        "notes": row.get("Notes"),
        "citations": [{"quote": quote}] if quote else [],
    }


def _record(info_type: str, mapping: dict[str, str], row: dict[str, object]) -> dict[str, object]:
    rec: dict[str, object] = {"info_type": info_type, **_common(row)}
    for column, field in mapping.items():
        value = row.get(column)
        if field in _LIST_FIELDS:
            rec[field] = (
                [p.strip() for p in str(value).split("\n") if p.strip()] if value else []
            )
        else:
            rec[field] = value
    # The TBD sheet labels several rows in the Info Type column itself — "Bonus", "Payment
    # Terms", "Penalities" — leaving Item empty. Those are named backlog topics, not blank
    # rows, so the label is recovered rather than lost.
    if info_type == "uncategorised" and not rec.get("item"):
        rec["item"] = row.get("Contract Section") or row.get("Info Type")
    # "Included" is how the workbook prices a service the contract bundles at no charge. It is
    # a real pricing fact, not a missing amount, so it gets its own flag rather than a null.
    if info_type == "pricing_item":
        note = str(row.get("Notes") or "").strip().lower()
        amount = row.get("Amount")
        rec["included"] = note == "included" or str(amount or "").strip().lower() == "included"
        rec["amount"] = amount if isinstance(amount, int | float) else None
    return rec


def convert(path: pathlib.Path) -> dict[str, object]:
    wb = openpyxl.load_workbook(path, data_only=True)
    missing = [s for s in SHEETS if s not in wb.sheetnames]
    if missing:
        sys.exit(f"workbook is missing expected sheets: {missing}")

    doc_meta: dict[str, object] = {}
    records: list[dict[str, object]] = []
    per_sheet: dict[str, int] = {}

    for sheet, (info_type, mapping) in SHEETS.items():
        ws = wb[sheet]
        doc_meta.update({k: v for k, v in _doc_meta(ws).items() if v is not None})

        rows = list(ws.iter_rows(min_row=HEADER_ROW, values_only=True))
        header = [str(h).strip() if h is not None else "" for h in rows[0]]
        unknown = set(mapping) - set(header)
        if unknown:
            sys.exit(f"{sheet}: expected columns not found: {sorted(unknown)}")

        count = 0
        for raw in rows[1:]:
            if all(v is None for v in raw):
                continue
            row = {h: _clean(v) for h, v in zip(header, raw, strict=False) if h}
            records.append(_record(info_type, mapping, row))
            count += 1
        per_sheet[sheet] = count

    cited = sum(1 for r in records if r["citations"])
    return {
        "source": path.name,
        # What this fixture can and cannot prove, recorded here so nobody scores against it
        # wrongly. The workbook ships a "Context (Contract snipet)" column but every cell is
        # empty, so there is no evidence to compare against: our extraction quotes the contract
        # and the workbook does not. Score records, never citations.
        "scores": {
            "records": True,
            "citations": cited > 0,
            "note": (
                "Context column is empty in the source workbook; citation coverage is ours to "
                "exceed, not to match."
            ),
        },
        "doc_meta": doc_meta,
        "records": records,
        "counts": per_sheet,
        "total": len(records),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("workbook", type=pathlib.Path)
    ap.add_argument("out", type=pathlib.Path)
    args = ap.parse_args()

    data = convert(args.workbook)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    for sheet, n in data["counts"].items():
        print(f"  {n:>3}  {sheet}")
    print(f"  {data['total']:>3}  TOTAL -> {args.out}")
    print(f"\ndoc_meta: {data['doc_meta']}")


if __name__ == "__main__":
    main()
