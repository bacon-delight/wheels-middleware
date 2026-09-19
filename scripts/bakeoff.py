"""Read one contract with several models and report what each found, and what each cost.

A projection says Nova Lite reads a contract for three cents. Whether it reads it *correctly* is
not something arithmetic can answer, and the difference between a 3x saving and a 35x one is
worth about a dollar to establish.

    python scripts/bakeoff.py --contract "../Documents/Actual Contracts/Walmart-...pdf" \
        --profile sonnet --profile haiku --profile nova-lite

The baseline is whichever profile runs first: "no loss on pricing" is measured against what
Sonnet actually found on this contract today, not against a remembered number.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# Set before app.config is imported: get_settings caches on first use, and the default provider
# is a fallback chain that would quietly re-read the whole contract on Claude when a cheap model
# failed — producing a flattering score at twenty times the price.
os.environ["LLM_PROVIDER"] = "bedrock"

from score_extraction import _by_type, score_amounts, score_type  # noqa: E402

from app.extraction.service import extract_contract  # noqa: E402
from app.llm.bedrock_provider import BedrockProvider  # noqa: E402
from app.ocr.pymupdf_engine import PyMuPDFEngine  # noqa: E402

PROFILES: dict[str, tuple[str, str]] = {
    "sonnet": ("global.anthropic.claude-sonnet-4-6", "ap-south-2"),
    "haiku": ("global.anthropic.claude-haiku-4-5-20251001-v1:0", "ap-south-2"),
    "nova-2-lite": ("global.amazon.nova-2-lite-v1:0", "ap-south-1"),
    "nova-lite": ("apac.amazon.nova-lite-v1:0", "ap-south-1"),
    "nova-micro": ("apac.amazon.nova-micro-v1:0", "ap-south-1"),
}

TYPES = ("pricing_item", "sla_item", "reporting_requirement", "definition",
         "responsibility", "online_tool", "information_section", "signature")


def run_profile(name: str, parse, out_dir: pathlib.Path) -> dict:
    model, region = PROFILES[name]
    print(f"\n  {name} — {model} in {region}", flush=True)
    started = time.monotonic()
    result = extract_contract(
        source=pathlib.Path("."), provider=BedrockProvider(model, region), parse=parse
    )
    wall = time.monotonic() - started
    telemetry = result.telemetry()

    # A Bedrock reroute or a stray fallback would show up as somebody else's model id, and a
    # great-looking score for a dollar is exactly the outcome this whole exercise must avoid.
    served = {c.model for c in result.calls if c.model}
    if served - {model}:
        raise SystemExit(f"!! {name} was served by {served}, not {model}")

    blob = {
        "profile": name, "model": model, "region": region,
        "extraction": result.extraction.model_dump(mode="json"),
        "telemetry": telemetry,
        "wall_seconds": round(wall, 1),
    }
    (out_dir / f"{name}.json").write_text(json.dumps(blob, indent=2, default=str))
    calls = telemetry.get("calls", [])
    windows = len({c.get("window") for c in calls})
    print(
        f"     {len(calls)} calls over {windows} window(s), {telemetry.get('records')} records, "
        f"${telemetry.get('cost_usd') or 0:.4f}, {wall:.0f}s",
        flush=True,
    )
    for c in calls:
        if c.get("error"):
            print(f"     ! {c['call']} w{c.get('window')}: {c['error'][:90]}", flush=True)
    return blob


def score(blob: dict, truth: dict, baseline: dict | None) -> dict:
    ours = blob["extraction"]["records"]
    got, expected = _by_type(ours), _by_type(truth["records"])
    out: dict = {"profile": blob["profile"], "per_type": {}}
    for t in TYPES:
        want = expected.get(t) or []
        if not want:
            continue
        s = score_type(t, want, got.get(t, []), ours)
        out["per_type"][t] = {
            "recall": s["recall"], "found": s["found"], "expected": s["expected"]
        }
    pricing = score_type(
        "pricing_item", expected["pricing_item"], got.get("pricing_item", []), ours
    )
    out["amounts"] = score_amounts(pricing.get("pairs") or [])
    out["records"] = len(ours)
    out["cost_usd"] = blob["telemetry"].get("cost_usd")
    out["wall_seconds"] = blob["wall_seconds"]
    out["truncated"] = blob["telemetry"].get("truncated_calls") or []
    out["failed"] = blob["telemetry"].get("failed_calls") or []

    if baseline is not None:
        base_records = baseline["extraction"]["records"]
        base_got = _by_type(base_records)
        base_pricing = score_type(
            "pricing_item", expected["pricing_item"], base_got.get("pricing_item", []),
            base_records,
        )
        out["vs_baseline"] = {
            "pricing_recall": pricing["recall"] - base_pricing["recall"],
            "amounts_compared": out["amounts"]["compared"]
            - score_amounts(base_pricing.get("pairs") or [])["compared"],
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--contract", required=True)
    ap.add_argument("--ground-truth", default="tests/fixtures/walmart_ground_truth.json")
    ap.add_argument("--profile", action="append", required=True, choices=list(PROFILES))
    ap.add_argument("--out", default="runs")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(exist_ok=True)
    truth = json.loads(pathlib.Path(args.ground_truth).read_text())

    # Parsed once, so every profile answers about byte-identical input and the only thing that
    # differs between them is the model.
    print(f"parsing {pathlib.Path(args.contract).name} ...", flush=True)
    parse = PyMuPDFEngine().parse(pathlib.Path(args.contract), tables=True)
    print(f"{parse.page_count} pages", flush=True)

    scores, baseline = [], None
    for name in args.profile:
        try:
            blob = run_profile(name, parse, out_dir)
        except Exception as e:  # noqa: BLE001 - one model failing should not end the bake-off
            print(f"     !! {name} did not finish: {type(e).__name__}: {e}", flush=True)
            continue
        if baseline is None:
            baseline = blob
        scores.append(score(blob, truth, baseline if blob is not baseline else None))

    print("\n" + "=" * 96)
    head = f"{'profile':<13}{'$':>8}{'x cheaper':>11}{'secs':>6}{'recs':>6}"
    for t in ("pricing_item", "sla_item", "reporting_requirement", "definition"):
        head += f"{t.split('_')[0][:5]:>7}"
    print(head + f"{'amounts':>9}")
    base_cost = scores[0]["cost_usd"] if scores and scores[0]["cost_usd"] else None
    for s in scores:
        cost = s["cost_usd"] or 0
        ratio = f"{base_cost / cost:.1f}x" if base_cost and cost else "-"
        row = (f"{s['profile']:<13}{cost:>8.4f}{ratio:>11}{s['wall_seconds']:>6.0f}"
               f"{s['records']:>6}")
        for t in ("pricing_item", "sla_item", "reporting_requirement", "definition"):
            r = s["per_type"].get(t, {}).get("recall")
            row += f"{r:>6.0%} " if r is not None else f"{'-':>7}"
        a = s["amounts"]
        row += f"{a['agree']:>5}/{a['compared']}"
        print(row)
        if s["truncated"] or s["failed"]:
            print(f"{'':<13}truncated={s['truncated']} failed={s['failed']}")
    print("=" * 96)
    print("\nrecall is against the hand-made workbook; amounts are agreed/compared.")
    print(f"runs written to {out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
