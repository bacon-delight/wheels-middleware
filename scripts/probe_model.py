"""Ask a Bedrock model the six questions that decide whether it can run this pipeline.

Cheap, fast and read-only: two pages of contract, one forced tool call per question. It is worth
running before any of the expensive work because a "no" on the first question ends the project —
the extractor has no text-output path, so a model that cannot be forced into a named tool cannot
serve it at all.

    python scripts/probe_model.py --model apac.amazon.nova-lite-v1:0 --region ap-south-1
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Before anything imports app.config, which caches its settings on first use.
os.environ.setdefault("LLM_PROVIDER", "bedrock")

from app.extraction.prompts import SYSTEM_PROMPT, build_document_prefix  # noqa: E402
from app.llm.base import Tool  # noqa: E402
from app.llm.bedrock_provider import BedrockProvider  # noqa: E402
from app.llm.tools import CONTRACT_TOOL  # noqa: E402
from app.ocr.pymupdf_engine import PyMuPDFEngine  # noqa: E402

SAMPLE = pathlib.Path(__file__).resolve().parent.parent / "tests/fixtures/MSA_MeridianFoods.pdf"

# A schema with none of the features the real one leans on, to tell "this model cannot do tools"
# apart from "this model cannot do THIS tool".
SIMPLE_TOOL = Tool(
    name="record_contract_terms",
    description="Record the terms found in the contract.",
    input_schema={
        "type": "object",
        "properties": {
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "info_type": {"type": "string"},
                        "program": {"type": "string"},
                        "item": {"type": "string"},
                        "amount": {"type": "number"},
                    },
                },
            }
        },
    },
)


def _ok(label: str, passed: bool, detail: str = "") -> None:
    mark = "  ok  " if passed else " FAIL "
    print(f"[{mark}] {label}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("--pages", type=int, default=2)
    args = ap.parse_args()

    print(f"\nprobing {args.model} in {args.region}\n" + "=" * 68)
    parse = PyMuPDFEngine().parse(SAMPLE, tables=True)
    prefix = build_document_prefix(parse.pages[: args.pages])
    provider = BedrockProvider(args.model, args.region)
    print(f"sample: {SAMPLE.name}, {args.pages} pages, ~{len(prefix) // 4:,} tokens of prefix")
    print(f"real tool schema: {len(json.dumps(CONTRACT_TOOL.input_schema)):,} bytes\n")

    # 1. Forced tool choice with a trivial schema. If this fails, nothing else matters.
    try:
        r = provider.call_tool(
            system=SYSTEM_PROMPT, user_text="List any priced item you can see.",
            tool=SIMPLE_TOOL, cache_prefix=prefix, max_tokens=1500,
        )
        _ok("forced toolChoice, simple schema", True,
            f"{len(r.data.get('records') or [])} records, stop_reason={r.stop_reason!r}")
        simple_ok = True
    except Exception as e:  # noqa: BLE001 - the answer is the exception
        _ok("forced toolChoice, simple schema", False, f"{type(e).__name__}: {e}")
        simple_ok = False

    # 2. The real schema: $defs, $ref, oneOf and a nine-way discriminator.
    try:
        r2 = provider.call_tool(
            system=SYSTEM_PROMPT, user_text="Emit every priced item on these pages.",
            tool=CONTRACT_TOOL, cache_prefix=prefix, max_tokens=2000,
        )
        recs = r2.data.get("records")
        kind = type(recs).__name__
        n = len(recs) if isinstance(recs, list) else "n/a"
        _ok("real 15.8 KB discriminated-union schema", True,
            f"records came back as {kind}, {n} of them")
        print(f"         first record: {json.dumps((recs or [{}])[0])[:180]}"
              if isinstance(recs, list) and recs else "         (no records)")
    except Exception as e:  # noqa: BLE001
        _ok("real 15.8 KB discriminated-union schema", False, f"{type(e).__name__}: {e}")

    # 3. What does truncation actually look like? Ask for far more than the budget allows.
    try:
        r3 = provider.call_tool(
            system=SYSTEM_PROMPT,
            user_text="Emit two hundred records covering everything here, in full detail.",
            tool=SIMPLE_TOOL if not simple_ok else CONTRACT_TOOL, cache_prefix=prefix,
            max_tokens=200,
        )
        print(f"[ note ] truncation: stop_reason={r3.stop_reason!r}, "
              f"output_tokens={r3.output_tokens}, truncated_flag={r3.truncated}")
        if not r3.truncated:
            _ok("truncation is detected", False,
                f"stop_reason {r3.stop_reason!r} is not the 'max_tokens' the code tests for — "
                "a cut-off answer would look like a short contract")
        else:
            _ok("truncation is detected", True)
    except Exception as e:  # noqa: BLE001
        print(f"[ note ] truncation probe raised {type(e).__name__}: {e}")

    # 4. Usage accounting, and whether a cachePoint was accepted at all.
    try:
        r4 = provider.call_tool(
            system=SYSTEM_PROMPT, user_text="Name the parties to this agreement.",
            tool=SIMPLE_TOOL, cache_prefix=prefix, max_tokens=400,
        )
        print(f"[ note ] usage: input={r4.input_tokens} output={r4.output_tokens} "
              f"cache_read={r4.cache_read_tokens} cache_write={r4.cache_write_tokens}")
        cached = (r4.cache_read_tokens or 0) + (r4.cache_write_tokens or 0)
        _ok("prompt caching reports figures", cached > 0,
            f"{cached:,} cached tokens" if cached else
            "none reported — either unsupported, or the prefix is under the model's minimum")
    except Exception as e:  # noqa: BLE001
        _ok("usage accounting", False, f"{type(e).__name__}: {e}")

    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
