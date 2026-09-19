"""Regenerate billing configurations so every charge names the clause it came from.

A config written before `config_builder` carried `record_id`, `document_id` and `citations`
is a list of amounts with nothing to check them against — the audit screen can show the
numbers but not the contract beside them, which is the whole point of the screen.

Rebuilding is safe because it reads the same stored terms the original run read: an engagement
whose terms have not changed gets the identical amounts back, now with their provenance. That
is also why this defaults to the engagements awaiting audit, and needs `--include-active` to
touch a live one — a live config only *should* be identical, and "should" is not a reason to
rewrite what is billing a customer today.

    TABLE_NAME=wheels-dev python scripts/rebuild_billing_config.py --apply
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.billing.config_builder import build_billing_config  # noqa: E402
from app.billing.estimate import compute_monthly_recurring  # noqa: E402
from app.store.repository import Repository  # noqa: E402
from app.store.s3 import S3Store  # noqa: E402

AUDIT_STATUSES = ("BILLING_SETUP", "PENDING_BILLING_AUDIT", "CHANGES_REQUESTED_AUDIT")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--table", default="wheels-dev")
    ap.add_argument("--vehicles-table", default="wheels-dev-vehicles")
    ap.add_argument("--bucket", default="wheels-dev-docs-432417416277")
    ap.add_argument("--include-active", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    repo = Repository(table_name=args.table, vehicles_table_name=args.vehicles_table)
    s3 = S3Store(bucket=args.bucket)
    wanted = AUDIT_STATUSES + (("ACTIVE",) if args.include_active else ())
    print(f"[{'APPLY' if args.apply else 'DRY RUN'}] {args.table} · {', '.join(wanted)}\n")

    touched = 0
    for engagement in repo.list_all_engagements():
        eid = engagement.engagement_id
        for sub in repo.list_submissions(eid):
            if sub.status.value not in wanted:
                continue
            config = (
                build_billing_config(repo, eid, sub.submission_id, s3=s3)
                if args.apply
                else None
            )
            if config is None:
                print(f"  would rebuild  {eid}  {sub.submission_id}  ({sub.status.value})")
                touched += 1
                continue
            items = config.get("pricing_items") or []
            traced = sum(1 for i in items if i.get("citations"))
            monthly = compute_monthly_recurring(config, engagement.fleet_size)
            # The stored figure is what bills; if a rebuild moves it, that is a real change in
            # the terms since, and worth seeing rather than silently writing over.
            drift = (
                ""
                if engagement.monthly_recurring is None
                or abs(engagement.monthly_recurring - monthly) < 0.01
                else f"  ** was {engagement.monthly_recurring}, now {monthly} **"
            )
            print(
                f"  rebuilt  {eid}  {sub.submission_id}  "
                f"{traced}/{len(items)} charges traced to a clause{drift}"
            )
            touched += 1

    if not touched:
        print("nothing to rebuild")
    elif not args.apply:
        print("\n(dry run; pass --apply to write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
