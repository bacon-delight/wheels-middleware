"""Move submissions off the three statuses the five-step lifecycle retired.

`Submission.status` is a validated enum, so a row left in a removed status cannot be loaded at
all — not shown as odd, not defaulted: `model_validate` raises and every read of that engagement
fails. There are no stored rows to be relaxed about.

The mapping, and why:

    PENDING_FINANCE_APPROVAL -> BILLING_SETUP   the customer had signed and a person had still
                                                to look; now what they look at is the billing,
                                                so it has to be generated first
    FINANCE_APPROVED         -> BILLING_SETUP   approved-but-not-yet-billed is the same place
    CHANGES_REQUESTED_FINANCE-> CHANGES_REQUESTED_AUDIT   the same rejection, renamed

Anything that lands in BILLING_SETUP then has its billing built, which carries it on to
PENDING_BILLING_AUDIT where a person can actually act on it. A row left in BILLING_SETUP with no
config would sit on the lifecycle board with nothing to audit.

    TABLE_NAME=wheels-dev python scripts/migrate_lifecycle.py --apply
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from boto3.dynamodb.conditions import Attr  # noqa: E402

from app.store import keys as k  # noqa: E402
from app.store.repository import Repository, utcnow  # noqa: E402

RETIRED = {
    "PENDING_FINANCE_APPROVAL": "BILLING_SETUP",
    "FINANCE_APPROVED": "BILLING_SETUP",
    "CHANGES_REQUESTED_FINANCE": "CHANGES_REQUESTED_AUDIT",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--table", default="wheels-dev")
    ap.add_argument("--vehicles-table", default="wheels-dev-vehicles")
    ap.add_argument("--bucket", default="wheels-dev-docs-432417416277")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    repo = Repository(table_name=args.table, vehicles_table_name=args.vehicles_table)
    print(f"[{'APPLY' if args.apply else 'DRY RUN'}] {args.table}\n")

    # Read raw: the rows in question cannot be loaded through the model, which is the problem.
    found: list[dict] = []
    kwargs = {
        "FilterExpression": Attr("type").eq("SUBMISSION") & Attr("status").is_in(list(RETIRED)),
    }
    while True:
        r = repo.table.scan(**kwargs)
        found += r.get("Items", [])
        lek = r.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek

    if not found:
        print("nothing to migrate")
        return 0

    billed = []
    for item in found:
        old = item["status"]
        new = RETIRED[old]
        eid, sid = item["engagement_id"], item["submission_id"]
        print(f"  {eid}  {sid}  {old} -> {new}")
        if not args.apply:
            continue
        repo.table.update_item(
            Key={"PK": k.eng_pk(eid), "SK": k.submission_sk(sid)},
            UpdateExpression="SET #s = :s, #g = :g, updated_at = :now",
            ExpressionAttributeNames={"#s": "status", "#g": "GSI1PK"},
            ExpressionAttributeValues={
                ":s": new, ":g": k.lcstatus_gsi1pk(new), ":now": utcnow()
            },
        )
        # The engagement carries a denormalised copy that lists and the board read.
        if repo.get_engagement(eid) and not repo.live_submission(eid):
            repo.set_engagement_status(eid, new)
        if new == "BILLING_SETUP":
            billed.append((eid, sid))

    for eid, sid in billed:
        from app.billing.config_builder import build_billing_config, ensure_schedule
        from app.billing.estimate import compute_monthly_recurring
        from app.store.s3 import S3Store

        s3 = S3Store(bucket=args.bucket)
        config = build_billing_config(repo, eid, sid, s3=s3)
        engagement = repo.get_engagement(eid)
        fleet = engagement.fleet_size if engagement else 100
        repo.set_engagement_billing(eid, fleet, compute_monthly_recurring(config, fleet))
        engagement = repo.get_engagement(eid)
        if engagement:
            ensure_schedule(repo, engagement, sid, config)
        repo.update_submission_status(eid, sid, "BILLING_SETUP", "PENDING_BILLING_AUDIT")
        print(f"  billing built for {eid}; now awaiting its audit")

    if not args.apply:
        print("\n(dry run; pass --apply to write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
