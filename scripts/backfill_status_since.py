"""Give existing engagements the timestamp their lifecycle status has always implied.

"How long has this been sitting here" was answered with `created_at`, which is the age of the
deal, not of the wait: an engagement created a year ago and signed yesterday read as 365 days
waiting. `status_since` is written on every status change from now on; this fills it in for
rows that predate it.

The source is the submission's own `updated_at` — the row the status was copied from, stamped
at the moment it last moved. Where there is no submission at all, the engagement has never
moved, so its creation is when its status began.

    TABLE_NAME=wheels-dev python scripts/backfill_status_since.py --apply
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.lifecycle.amendment import cycle_in_force, ordered_cycles  # noqa: E402
from app.store import keys as k  # noqa: E402
from app.store.repository import Repository  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--table", default="wheels-dev")
    ap.add_argument("--vehicles-table", default="wheels-dev-vehicles")
    ap.add_argument("--force", action="store_true", help="rewrite rows that already have one")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    repo = Repository(table_name=args.table, vehicles_table_name=args.vehicles_table)
    print(f"[{'APPLY' if args.apply else 'DRY RUN'}] {args.table}\n")

    done = skipped = 0
    for e in repo.list_all_engagements():
        if e.status_since and not args.force:
            skipped += 1
            continue
        cycles = repo.list_submissions(e.engagement_id)
        # The cycle the engagement's status was copied from: the live one where there is one,
        # else the latest. An amendment under review does not move a live engagement's status,
        # so reading the newest cycle unconditionally would date it from the wrong thing.
        sub = cycle_in_force(cycles) or (ordered_cycles(cycles)[-1] if cycles else None)
        since = (sub.updated_at if sub else None) or e.created_at
        print(f"  {e.engagement_id}  {e.status:24} since {since}")
        if args.apply:
            repo.table.update_item(
                Key={"PK": k.eng_pk(e.engagement_id), "SK": k.engagement_meta_sk()},
                UpdateExpression="SET status_since = :s",
                ExpressionAttributeValues={":s": since},
            )
        done += 1

    print(f"\n{done} filled in, {skipped} already had one")
    if not args.apply:
        print("(dry run; pass --apply to write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
