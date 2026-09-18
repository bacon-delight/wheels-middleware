"""Backfill: turn each engagement's free-text `client_name` into a real Customer record.

Idempotent — safe to re-run. Dry-run by default; pass --apply to write.

    python scripts/backfill_customers.py --table wheels-dev --region ap-south-2
    python scripts/backfill_customers.py --table wheels-dev --region ap-south-2 --apply

Why the fleet override matters: `fleet_size` is now derived from the count of vehicles
assigned to an engagement. No vehicle records exist yet, so without seeding
`fleet_size_override` from the current `fleet_size`, the first write to an ACTIVE engagement
would recompute its dues from zero vehicles and wipe out real billing. Seeding the override
preserves today's number exactly; a provider can clear it later to opt into the derived count.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3  # noqa: E402

from app.store.models import Customer, EngagementScope  # noqa: E402
from app.store.repository import Repository, new_id, utcnow  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default="wheels-dev")
    ap.add_argument("--vehicles-table", default="wheels-dev-vehicles")
    ap.add_argument("--region", default="ap-south-2")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    ddb = boto3.resource("dynamodb", region_name=args.region)
    repo = Repository(
        table_name=args.table, resource=ddb, vehicles_table_name=args.vehicles_table
    )
    verb = "APPLY" if args.apply else "DRY RUN"
    print(f"[{verb}] table={args.table} region={args.region}\n")

    by_name = {c.legal_name.strip().lower(): c for c in repo.list_customers()}
    engagements = repo.list_all_engagements()
    print(f"found {len(engagements)} engagements, {len(by_name)} existing customers\n")

    for e in engagements:
        name = (e.client_name or "").strip()
        if not name:
            print(f"  ! {e.engagement_id} ({e.name}) has no client_name — skipped")
            continue

        customer = by_name.get(name.lower())
        if customer is None:
            customer = Customer(
                customer_id=new_id(), legal_name=name, display_name=e.name,
                created_by="backfill", created_at=utcnow(),
            )
            print(f"  + customer {customer.customer_id}  {name!r}")
            if args.apply:
                repo.put_customer(customer)
            by_name[name.lower()] = customer
        else:
            print(f"  = customer {customer.customer_id}  {name!r} (exists)")

        if e.customer_id == customer.customer_id and e.fleet_size_override is not None:
            print(f"  = {e.engagement_id} ({e.name}) already linked — skipped\n")
            continue

        override = e.fleet_size_override
        if override is None:
            # Preserve today's billed number; see the module docstring.
            override = e.fleet_size or 0
        print(
            f"  ~ {e.engagement_id} ({e.name}) -> customer={customer.customer_id} "
            f"scope={EngagementScope.LEASE_AND_SERVICE.value} fleet_size_override={override}\n"
        )
        if args.apply:
            repo.set_engagement_customer(e.engagement_id, customer.customer_id, name)
            repo.set_engagement_scope(e.engagement_id, EngagementScope.LEASE_AND_SERVICE.value)
            repo.set_fleet_override(e.engagement_id, override)

    # Materialize the document slot map so nothing depends on the legacy scalar fields.
    print("submissions:")
    for sub in repo.list_all_submissions():
        if sub.document_ids:
            print(f"  = {sub.submission_id} already has document_ids")
            continue
        slots = sub.docs()
        if not slots:
            print(f"  . {sub.submission_id} has no documents yet")
            continue
        print(f"  ~ {sub.submission_id} -> document_ids={slots}")
        if args.apply:
            fresh = repo.get_submission(sub.engagement_id, sub.submission_id)
            if fresh is not None:
                fresh.document_ids = slots
                repo.put_submission(fresh)

    if not args.apply:
        print("\nnothing written — re-run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
