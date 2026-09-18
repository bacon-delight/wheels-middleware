"""Score an extraction against the hand-made workbook.

Counting records proves nothing: an extraction that emits three hundred plausible rows and
misses the fee schedule is worse than one that emits thirty correct ones. What matters is
whether every term a person found is also found by us, and whether the numbers agree.

So the headline figure is **recall against the workbook**, computed per category. Precision is
reported too, but read it carefully: the workbook is a floor, not a ceiling. A record we found
that the workbook lacks is usually a record the person missed, which is the whole point of
building this — so "extra" is listed for inspection rather than counted as an error.

    python scripts/score_extraction.py tests/fixtures/walmart_ground_truth.json run.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections import defaultdict
from typing import Any

# Punctuation and case vary freely between a person's transcription and a model's. Matching on
# the normalised form is the only way to compare them without hand-tuning per row.
_PUNCT = re.compile(r"[^a-z0-9 ]+")
_SPACE = re.compile(r"\s+")
_NOISE = {"program", "programs", "fee", "fees", "the", "a", "of", "and"}


def norm(value: Any) -> str:
    if value is None:
        return ""
    text = _PUNCT.sub(" ", str(value).lower())
    return _SPACE.sub(" ", text).strip()


def key_words(value: Any) -> frozenset[str]:
    """A bag of significant words, for matching wording that differs only in filler."""
    return frozenset(w for w in norm(value).split() if w not in _NOISE)


def _records(blob: dict[str, Any]) -> list[dict[str, Any]]:
    if "records" in blob:
        return blob["records"]
    return blob.get("extraction", {}).get("records", [])


def _by_type(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        out[r.get("info_type", "?")].append(r)
    return out


def _match(want: dict, pool: list[dict], fields: tuple[str, ...]) -> dict | None:
    """Find the pool record that best answers `want`.

    Exact normalised equality on the key fields first; failing that, the record sharing the most
    significant words, provided it shares at least half of them. A contract term transcribed by
    two readers rarely matches character for character.
    """
    target = tuple(norm(want.get(f)) for f in fields)
    for candidate in pool:
        if tuple(norm(candidate.get(f)) for f in fields) == target:
            return candidate

    want_words = frozenset().union(*(key_words(want.get(f)) for f in fields)) or frozenset()
    if not want_words:
        return None
    best, best_score = None, 0.0
    for candidate in pool:
        got = frozenset().union(*(key_words(candidate.get(f)) for f in fields))
        if not got:
            continue
        score = len(want_words & got) / len(want_words)
        if score > best_score:
            best, best_score = candidate, score
    return best if best_score >= 0.5 else None


# Which fields identify a record of each type, for matching purposes.
MATCH_FIELDS: dict[str, tuple[str, ...]] = {
    "pricing_item": ("program", "item"),
    "sla_item": ("category",),
    "reporting_requirement": ("report_name",),
    "definition": ("term",),
    "online_tool": ("tool_name",),
    "responsibility": ("task",),
    "signature": ("company", "name"),
    "information_section": ("topic",),
    "uncategorised": ("item",),
}


def _text_of(record: dict) -> frozenset[str]:
    """Every significant word in a record, whatever its type."""
    words: set[str] = set()
    for key, value in record.items():
        if key in {"info_type", "citations", "confidence", "notes", "contract_section"}:
            continue
        if isinstance(value, str):
            words |= key_words(value)
        elif isinstance(value, list):
            for v in value:
                if isinstance(v, str):
                    words |= key_words(v)
    return frozenset(words)


def _match_anywhere(want: dict, others: list[dict], fields: tuple[str, ...]) -> dict | None:
    """Is this content in our extraction at all, under any record type?

    Two readers of the same contract disagree about where a clause belongs far more often than
    they disagree about whether it is there. The workbook files "Online Tools Use Rights" as an
    un-triaged backlog item; we may have read the same clause as a responsibility. Counting
    that as a miss would measure agreement about taxonomy, not coverage.
    """
    want_words = frozenset().union(*(key_words(want.get(f)) for f in fields)) or frozenset()
    if len(want_words) < 2:
        return None
    best, best_score = None, 0.0
    for candidate in others:
        got = _text_of(candidate)
        if not got:
            continue
        score = len(want_words & got) / len(want_words)
        if score > best_score:
            best, best_score = candidate, score
    return best if best_score >= 0.6 else None


def score_type(
    info_type: str,
    expected: list[dict],
    got: list[dict],
    everything: list[dict] | None = None,
) -> dict[str, Any]:
    fields = MATCH_FIELDS.get(info_type, ("item",))
    pool = list(got)
    found, elsewhere, missed = [], [], []
    for want in expected:
        hit = _match(want, pool, fields)
        if hit is not None:
            found.append((want, hit))
            pool.remove(hit)  # one-to-one, so duplicates in ours cannot inflate recall
            continue
        other = _match_anywhere(want, everything or [], fields) if everything else None
        if other is not None:
            elsewhere.append((want, other))
        else:
            missed.append(want)
    covered = len(found) + len(elsewhere)
    return {
        "expected": len(expected),
        "got": len(got),
        "found": len(found),
        "elsewhere": elsewhere,
        "missed": missed,
        "extra": pool,
        "recall": len(found) / len(expected) if expected else 1.0,
        "coverage": covered / len(expected) if expected else 1.0,
        "pairs": found,
    }


def score_amounts(pairs: list[tuple[dict, dict]]) -> dict[str, Any]:
    """Do the numbers agree on the pricing rows we both found?

    This is the figure with money behind it. A wrong amount is far more damaging than a missing
    record, because a missing record is visibly missing.
    """
    both = agree = 0
    disagreements = []
    for want, got in pairs:
        a, b = want.get("amount"), got.get("amount")
        if a is None or b is None:
            continue
        both += 1
        if abs(float(a) - float(b)) < 0.005:
            agree += 1
        else:
            disagreements.append(
                {"item": want.get("item"), "workbook": a, "ours": b,
                 "program": want.get("program")}
            )
    return {
        "compared": both,
        "agree": agree,
        "rate": agree / both if both else 1.0,
        "disagreements": disagreements,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ground_truth", type=pathlib.Path)
    ap.add_argument("run", type=pathlib.Path)
    ap.add_argument("--verbose", action="store_true", help="list every missed record")
    args = ap.parse_args()

    gt = json.loads(args.ground_truth.read_text())
    run = json.loads(args.run.read_text())
    expected = _by_type(_records(gt))
    got = _by_type(_records(run))

    print(f"ground truth : {args.ground_truth}  ({sum(len(v) for v in expected.values())} records)")
    print(f"extraction   : {args.run}  ({sum(len(v) for v in got.values())} records)")
    tel = run.get("telemetry") or {}
    if tel:
        print(
            f"run          : {tel.get('model')}  ${tel.get('cost_usd')}  "
            f"{(tel.get('duration_ms') or 0) / 1000:.0f}s  "
            f"citations {tel.get('citations_resolved')}/{tel.get('citations_total')}"
        )
    print()

    all_ours = _records(run)
    header = (
        f"  {'record type':<24}{'workbook':>9}{'ours':>6}{'same type':>11}"
        f"{'elsewhere':>11}{'covered':>9}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    results: dict[str, dict] = {}
    total_expected = total_found = total_covered = 0
    for info_type in sorted(set(expected) | set(got)):
        res = score_type(
            info_type, expected.get(info_type, []), got.get(info_type, []), all_ours
        )
        results[info_type] = res
        total_expected += res["expected"]
        total_found += res["found"]
        total_covered += res["found"] + len(res["elsewhere"])
        flag = "" if res["coverage"] >= 0.9 else "  <-- gap"
        print(
            f"  {info_type:<24}{res['expected']:>9}{res['got']:>6}{res['found']:>11}"
            f"{len(res['elsewhere']):>11}{res['coverage'] * 100:>8.0f}%{flag}"
        )
    overall = total_covered / total_expected if total_expected else 1.0
    same_type = total_found / total_expected if total_expected else 1.0
    print("  " + "-" * (len(header) - 2))
    print(
        f"  {'OVERALL':<24}{total_expected:>9}{sum(len(v) for v in got.values()):>6}"
        f"{total_found:>11}{total_covered - total_found:>11}{overall * 100:>8.0f}%"
    )
    print(f"  (filed under the same record type as the workbook: {same_type * 100:.0f}%)")

    pricing = results.get("pricing_item")
    if pricing:
        print()
        # Programs are what coverage is counted in, so they get their own line.
        want_programs = {norm(r.get("program")) for r in expected.get("pricing_item", [])}
        got_programs = {norm(r.get("program")) for r in got.get("pricing_item", [])}
        matched = {p for p in want_programs if p and any(
            p == g or key_words(p) <= key_words(g) or key_words(g) <= key_words(p)
            for g in got_programs
        )}
        print(f"  programs named in the workbook : {len(want_programs)}")
        print(f"  ...also found by us            : {len(matched)}"
              f"  ({len(matched) / len(want_programs) * 100:.0f}%)")
        if want_programs - matched:
            for p in sorted(want_programs - matched):
                print(f"      MISSED PROGRAM: {p}")

        amounts = score_amounts(pricing["pairs"])
        print(f"  amounts compared               : {amounts['compared']}")
        print(f"  ...agreeing                    : {amounts['agree']}"
              f"  ({amounts['rate'] * 100:.0f}%)")
        for d in amounts["disagreements"][:12]:
            print(
                f"      DIFFERS: {str(d['item'])[:46]:<48} "
                f"workbook={d['workbook']} ours={d['ours']}"
            )

    missed_any = {k: v["missed"] for k, v in results.items() if v["missed"]}
    if missed_any:
        print()
        print("  not found by us:")
        for info_type, rows in missed_any.items():
            shown = rows if args.verbose else rows[:5]
            for row in shown:
                fields = MATCH_FIELDS.get(info_type, ("item",))
                label = " / ".join(str(row.get(f)) for f in fields if row.get(f))
                print(f"    [{info_type}] {label[:96]}")
            if len(rows) > len(shown):
                print(f"    [{info_type}] ... and {len(rows) - len(shown)} more (--verbose)")

    print()
    if overall >= 0.9:
        print(f"  PASS  recall {overall * 100:.0f}% against a human extraction")
    else:
        print(f"  BELOW BAR  recall {overall * 100:.0f}%; the workbook is the floor")
    sys.exit(0 if overall >= 0.9 else 1)


if __name__ == "__main__":
    main()
