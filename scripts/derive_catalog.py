"""Derive the service catalog from the real contract corpus.

Coverage — "19 of 35 programs availed" — is only meaningful if the denominator is real. So the
catalog is mined from the contracts Wheels has actually signed rather than invented, and the
result is written to a JSON file for a person to read before it is ever loaded. The clustering
here is an input to the catalog, not the catalog.

Two sources, because the corpus is not uniform:

* **Table headings**, mined deterministically from every contract. Fast, free and precise: a
  pricing schedule names its programs in its own heading rows.
* **A pricing extraction pass** over a sample of contracts, which is the only way to see the
  programs in documents that set their pricing out in prose. The AbbVie master agreement yields
  no tables at all, so a table-only catalog would quietly omit whatever only it sells.

Names are then clustered with the same normalisation the runtime resolver uses, so a name that
clusters here will resolve there. The canonical form is the one used by the most *contracts* —
not the most rows, or one contract with forty fuel lines would name the whole family.

    python scripts/derive_catalog.py ../Documents/"Actual Contracts" \
        -o app/catalog/seed_catalog.json
    python scripts/derive_catalog.py ... --llm-sample 8     # also read prose contracts
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.catalog.resolve import normalise, slugify  # noqa: E402

# A heading row in a pricing schedule: an all-caps program name, sometimes carrying the section
# it refers to. The trailing reference is stripped; stray single letters are bleed from the
# neighbouring cell and are dropped.
_HEADING = re.compile(r"^([A-Z][A-Z0-9&/\-–()' ,.]{6,80}?PROGRAM)\b")
_SECTION_REF = re.compile(r"\s*\(SECTION[^)]*\)\s*$", re.IGNORECASE)
_LEADING_JUNK = re.compile(r"^(?:[A-Z]\s+)+(?=[A-Z][a-z])")


def _clean_program(raw: str) -> str:
    name = re.sub(r"\s+", " ", raw).strip()
    name = _SECTION_REF.sub("", name)
    name = name.title()
    name = _LEADING_JUNK.sub("", name)
    # Title-casing mangles the acronyms that matter most in this domain.
    for wrong, right in (("Mvr", "MVR"), ("Dot", "DOT"), ("Ev ", "EV "), ("Sow", "SOW")):
        name = name.replace(wrong, right)
    return name.strip()


def mine_tables(pdf: pathlib.Path) -> tuple[set[str], set[tuple[str, str]]]:
    """Programs, and (program, item) pairs, read out of a contract's own tables."""
    import pymupdf

    programs: set[str] = set()
    items: set[tuple[str, str]] = set()
    doc = pymupdf.open(str(pdf))
    try:
        for page in doc:
            try:
                tables = page.find_tables().tables
            except Exception:  # noqa: BLE001 - an unreadable page still has other pages
                continue
            for table in tables:
                try:
                    rows = table.extract()
                except Exception:  # noqa: BLE001
                    continue
                current = ""
                for row in rows:
                    cells = [re.sub(r"\s+", " ", (c or "")).strip() for c in row]
                    heading = next(
                        (_clean_program(m.group(1)) for c in cells if (m := _HEADING.match(c))),
                        None,
                    )
                    if heading and len(heading) > 8:
                        current = heading
                        programs.add(current)
                        continue
                    label = cells[0] if cells else ""
                    if current and label and 4 < len(label) < 80 and not label.isupper():
                        items.add((current, re.sub(r"\s+", " ", label).strip()))
    finally:
        doc.close()
    return programs, items


def mine_llm(pdf: pathlib.Path) -> tuple[set[str], set[tuple[str, str]]]:
    """The pricing pass only, which is where programs are named."""
    from app.extraction.service import extract_contract

    result = extract_contract(str(pdf), calls=("pricing",))
    programs: set[str] = set()
    items: set[tuple[str, str]] = set()
    for record in result.extraction.by_type("pricing_item"):
        name = (record.program or "").strip()
        if not name:
            continue
        programs.add(name)
        if record.item:
            items.add((name, record.item.strip()))
    return programs, items


def cluster(counts: collections.Counter[str]) -> dict[str, dict[str, Any]]:
    """Group surface forms that normalise together; the commonest wins the canonical name.

    Counting distinct contracts rather than occurrences matters: "Fuel Management Program"
    appearing forty-one times in one agreement should not outvote fourteen agreements.
    """
    groups: dict[str, list[str]] = collections.defaultdict(list)
    for name in counts:
        groups[normalise(name)].append(name)

    out: dict[str, dict[str, Any]] = {}
    for key, names in groups.items():
        if not key:
            continue
        canonical = max(names, key=lambda n: (counts[n], -len(n)))
        out[key] = {
            "program_id": slugify(canonical),
            "name": canonical,
            "aliases": sorted({n for n in names if n != canonical}),
            "contract_count": counts[canonical],
        }
    return out


def recurate(path: pathlib.Path) -> None:
    """Apply the curation rules to a catalog that has already been mined.

    Editorial decisions change more often than the corpus does, and re-mining costs money and
    minutes. Idempotent, because the slugs are deterministic.
    """
    curation_path = pathlib.Path(__file__).resolve().parent.parent / "app/catalog/curation.json"
    curation = json.loads(curation_path.read_text())
    catalog = json.loads(path.read_text())
    merges = {normalise(k): (k, v) for k, v in (curation.get("merge") or {}).items()}
    dropped = {normalise(n) for n in curation.get("drop", [])}
    categories = {normalise(k): v for k, v in (curation.get("categories") or {}).items()}

    by_key = {normalise(p["name"]): p for p in catalog["programs"]}
    keep = []
    for program in catalog["programs"]:
        key = normalise(program["name"])
        if key in dropped:
            continue
        rule = merges.get(key)
        if rule:
            spelling, target = rule
            into = by_key.get(normalise(target))
            if into is not None and into is not program:
                into["aliases"] = sorted(set(into["aliases"]) | {program["name"], spelling})
                into["items"].extend(program["items"])
                into["contract_count"] += program.get("contract_count", 0)
                continue
        keep.append(program)

    for spelling, target in (curation.get("merge") or {}).items():
        into = by_key.get(normalise(target))
        if into is not None and spelling != into["name"]:
            into["aliases"] = sorted(set(into["aliases"]) | {spelling})
    for program in keep:
        program["category"] = categories.get(normalise(program["name"]), program.get("category"))
        seen: dict[str, dict] = {}
        for item in program["items"]:
            seen.setdefault(item["item_id"], item)
        program["items"] = sorted(seen.values(), key=lambda i: i["name"])

    catalog["programs"] = sorted(keep, key=lambda p: -p.get("contract_count", 0))
    path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n")
    total = sum(len(p["items"]) for p in catalog["programs"])
    print(f"re-curated: {len(catalog['programs'])} programs, {total} items -> {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("corpus", type=pathlib.Path, help="folder of contract PDFs")
    ap.add_argument(
        "-o", "--out", type=pathlib.Path,
        default=pathlib.Path("app/catalog/seed_catalog.json"),
    )
    ap.add_argument(
        "--recurate",
        action="store_true",
        help="re-apply curation.json to the existing catalog without mining the corpus again",
    )
    ap.add_argument(
        "--llm-sample",
        type=int,
        default=0,
        help="also run the pricing extraction over this many contracts, to catch prose-only ones",
    )
    args = ap.parse_args()

    if args.recurate:
        return recurate(args.out)

    pdfs = sorted(args.corpus.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"no PDFs under {args.corpus}")
    print(f"mining {len(pdfs)} contracts...")

    program_contracts: collections.Counter[str] = collections.Counter()
    item_contracts: collections.Counter[tuple[str, str]] = collections.Counter()
    sources: dict[str, set[str]] = collections.defaultdict(set)

    for pdf in pdfs:
        programs, items = mine_tables(pdf)
        for p in programs:
            program_contracts[p] += 1
            sources[p].add("table")
        for pair in items:
            item_contracts[pair] += 1
        print(f"  {pdf.name[:54]:<56} {len(programs):>3} programs  {len(items):>3} items")

    if args.llm_sample:
        # Prefer contracts that gave up no tables: those are exactly the ones a deterministic
        # pass cannot see, so they are where an extraction pass earns its cost.
        scored = sorted(pdfs, key=lambda p: len(mine_tables(p)[0]))
        chosen = scored[: args.llm_sample]
        print(f"\nreading {len(chosen)} contracts with the extractor (prose-heavy first)...")
        with ThreadPoolExecutor(max_workers=4) as pool:
            for pdf, (programs, items) in zip(
                chosen, pool.map(mine_llm, chosen), strict=False
            ):
                for p in programs:
                    program_contracts[p] += 1
                    sources[p].add("llm")
                for pair in items:
                    item_contracts[pair] += 1
                print(f"  {pdf.name[:54]:<56} {len(programs):>3} programs  {len(items):>3} items")

    # Editorial rules, kept out of the mining so the mining stays deterministic.
    curation_path = pathlib.Path(__file__).resolve().parent.parent / "app/catalog/curation.json"
    curation = json.loads(curation_path.read_text()) if curation_path.exists() else {}
    dropped = {normalise(n) for n in curation.get("drop", [])}
    merges = {normalise(k): v for k, v in (curation.get("merge") or {}).items()}
    categories = {normalise(k): v for k, v in (curation.get("categories") or {}).items()}

    folded: collections.Counter[str] = collections.Counter()
    alias_of: dict[str, str] = {}
    spelled = {normalise(k): k for k in (curation.get("merge") or {})}
    for name, n in program_contracts.items():
        key = normalise(name)
        if key in dropped:
            continue
        target = merges.get(key)
        if target:
            folded[target] += n
            alias_of[name] = target
        else:
            folded[name] += n
    # A merge target named by a rule but never mined on its own still belongs in the catalog.
    for target in set(merges.values()):
        folded.setdefault(target, 0)
    program_contracts = folded

    programs = cluster(program_contracts)
    for raw, target in list(merges.items()):
        # A merge rule names an alias whether or not the corpus happened to use it here. Adding
        # it regardless is what lets a later contract resolve a wording this run never saw.
        entry = programs.get(normalise(target))
        if entry is not None:
            alias_of.setdefault(spelled.get(raw, raw), target)
    for raw, target in alias_of.items():
        entry = programs.get(normalise(target))
        if entry and raw != entry["name"] and raw not in entry["aliases"]:
            entry["aliases"].append(raw)
    for entry in programs.values():
        entry["aliases"] = sorted(set(entry["aliases"]))
        entry["category"] = categories.get(normalise(entry["name"]))

    # Items belong to a program, because the same label means different things under different
    # ones: "Monthly Program Fee" appears under twenty-two programs in this corpus.
    by_program: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for (program, item), n in item_contracts.items():
        key = normalise(program)
        if key in dropped:
            continue
        key = normalise(merges.get(key, program))
        if key in programs:
            by_program[programs[key]["program_id"]][item] += n

    catalog: dict[str, Any] = {"programs": []}
    for entry in sorted(programs.values(), key=lambda e: -e["contract_count"]):
        items = cluster(by_program.get(entry["program_id"], collections.Counter()))
        catalog["programs"].append(
            {
                **entry,
                "source": "derived",
                "status": "ACTIVE",
                "items": [
                    {
                        "item_id": i["program_id"],
                        "name": i["name"],
                        "aliases": i["aliases"],
                    }
                    for i in sorted(items.values(), key=lambda x: x["name"])
                ],
            }
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n")

    total_items = sum(len(p["items"]) for p in catalog["programs"])
    print(f"\n{len(catalog['programs'])} programs, {total_items} items -> {args.out}")
    print("\ntop programs by contracts naming them:")
    for p in catalog["programs"][:18]:
        print(f"  {p['contract_count']:>2}  {p['name'][:50]:<52} {len(p['items']):>3} items")


if __name__ == "__main__":
    main()
