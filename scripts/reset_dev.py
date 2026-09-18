"""Wipe the dev environment, keeping one provider account.

Removes every DynamoDB item in both tables, every object in the docs bucket, and every Cognito
user except the one named by --keep. Dry-run by default; pass --apply to write.

    python scripts/reset_dev.py
    python scripts/reset_dev.py --apply

The kept user's profile and provider-directory rows are rebuilt afterwards so they can still
sign in and land on the dashboard.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3  # noqa: E402

from app.store.models import ProviderUser, UserProfile  # noqa: E402
from app.store.repository import Repository, utcnow  # noqa: E402

KEEP_DEFAULT = "dipanjan.de@logiforma.com"


def wipe_table(ddb, name: str, apply: bool) -> int:
    table = ddb.Table(name)
    keys = [k["AttributeName"] for k in table.key_schema]
    items, kwargs = [], {"ProjectionExpression": ", ".join(f"#k{i}" for i in range(len(keys))),
                         "ExpressionAttributeNames": {f"#k{i}": k for i, k in enumerate(keys)}}
    while True:
        r = table.scan(**kwargs)
        items.extend(r.get("Items", []))
        lek = r.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    print(f"  {name}: {len(items)} items")
    if apply and items:
        with table.batch_writer() as batch:
            for it in items:
                batch.delete_item(Key={k: it[k] for k in keys})
    return len(items)


def wipe_bucket(s3, bucket: str, apply: bool) -> int:
    total = 0
    token = None
    while True:
        kw = {"Bucket": bucket, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        r = s3.list_objects_v2(**kw)
        objs = [{"Key": o["Key"]} for o in r.get("Contents", [])]
        total += len(objs)
        if apply and objs:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objs})
        token = r.get("NextContinuationToken")
        if not r.get("IsTruncated"):
            break
    print(f"  {bucket}: {total} objects")
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="ap-south-2")
    ap.add_argument("--table", default="wheels-dev")
    ap.add_argument("--vehicles-table", default="wheels-dev-vehicles")
    ap.add_argument("--bucket", default="wheels-dev-docs-432417416277")
    ap.add_argument("--pool", default="ap-south-2_5IstyERCM")
    ap.add_argument("--keep", default=KEEP_DEFAULT)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    verb = "APPLY" if args.apply else "DRY RUN"
    print(f"[{verb}] keeping only {args.keep}\n")

    ddb = boto3.resource("dynamodb", region_name=args.region)
    s3 = boto3.client("s3", region_name=args.region)
    idp = boto3.client("cognito-idp", region_name=args.region)

    print("DynamoDB:")
    wipe_table(ddb, args.table, args.apply)
    wipe_table(ddb, args.vehicles_table, args.apply)

    print("S3:")
    wipe_bucket(s3, args.bucket, args.apply)

    print("Cognito:")
    kept = None
    users, token = [], None
    while True:
        kw = {"UserPoolId": args.pool, "Limit": 60}
        if token:
            kw["PaginationToken"] = token
        r = idp.list_users(**kw)
        users.extend(r["Users"])
        token = r.get("PaginationToken")
        if not token:
            break
    for u in users:
        attrs = {a["Name"]: a["Value"] for a in u["Attributes"]}
        email = attrs.get("email", "")
        if email.lower() == args.keep.lower():
            kept = {"user_id": attrs.get("sub") or u["Username"], "email": email,
                    "name": attrs.get("name"), "phone": attrs.get("phone_number")}
            print(f"  keep   {email}")
            continue
        print(f"  delete {email or u['Username']}")
        if args.apply:
            idp.admin_delete_user(UserPoolId=args.pool, Username=u["Username"])

    if kept is None:
        print(f"\n  ! {args.keep} not found in the pool — nothing was kept")
        return 1

    # The wipe removed the kept user's own rows; rebuild them so they can still sign in.
    if args.apply:
        repo = Repository(table_name=args.table, resource=ddb,
                          vehicles_table_name=args.vehicles_table)
        repo.put_user_profile(UserProfile(
            user_id=kept["user_id"], email=kept["email"], name=kept["name"] or "Dipanjan De",
            phone=kept["phone"] or "+918013758776", onboarded=True, updated_at=utcnow(),
        ))
        repo.put_provider_user(ProviderUser(
            user_id=kept["user_id"], email=kept["email"], name=kept["name"] or "Dipanjan De",
            phone=kept["phone"] or "+918013758776", onboarded=True, created_at=utcnow(),
        ))
        print(f"\n  restored profile + provider directory for {kept['email']}")

    if not args.apply:
        print("\nnothing written — re-run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
