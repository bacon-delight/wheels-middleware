"""Load the reviewed service catalog into DynamoDB.

Separate from derivation on purpose: mining produces a proposal, a person reads it, and only
then does it become the thing every coverage number is measured against.

    TABLE_NAME=wheels-dev python scripts/load_catalog.py --apply
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.catalog.store import SEED_PATH, seed_from_file  # noqa: E402
from app.store.repository import Repository  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", type=pathlib.Path, default=SEED_PATH)
    ap.add_argument("--apply", action="store_true", help="write; otherwise just summarise")
    args = ap.parse_args()

    data = json.loads(args.file.read_text())
    programs = data.get("programs", [])
    items = sum(len(p.get("items", [])) for p in programs)
    print(f"{args.file}: {len(programs)} programs, {items} items")
    if not args.apply:
        for p in programs[:12]:
            print(
                f"  {p.get('contract_count', 0):>2}  "
                f"{p['name'][:52]:<54} {len(p['items']):>3} items"
            )
        print("\n(dry run; pass --apply to write)")
        return

    repo = Repository()
    wrote_p, wrote_i = seed_from_file(repo, args.file)
    print(f"wrote {wrote_p} programs and {wrote_i} items")


if __name__ == "__main__":
    main()
