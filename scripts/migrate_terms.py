"""Move existing engagements onto the four-category term shape.

Customers, engagements, vehicles, payments and the audit trail are left completely alone. Only
the old per-service-line review rows go, replaced by terms in the new shape so that every
engagement — not just the two with a real contract behind them — shows pricing, service levels,
reporting and the rest, resolves against the catalog, and reports coverage.

An engagement whose documents have genuinely been extracted is skipped: its terms came from the
contract and must not be overwritten by synthetic ones.

    TABLE_NAME=wheels-dev python scripts/migrate_terms.py --apply
"""

from __future__ import annotations

import argparse
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from boto3.dynamodb.conditions import Key  # noqa: E402
from seed_dev import LEASE_PROGRAMS, PROGRAM_RATES, build_terms  # noqa: E402

from app.catalog.store import load_snapshot, seed_from_file  # noqa: E402
from app.store import keys as k  # noqa: E402
from app.store.repository import Repository  # noqa: E402


def drop_legacy_fields(repo: Repository, engagement_id: str, apply: bool) -> int:
    """Remove the old review-field rows for one engagement, and nothing else.

    Scoped by type as well as by key prefix: terms share the `DOC#` prefix, and deleting them
    here would undo the very thing this migration exists to create.
    """
    doomed = []
    kwargs = {
        "KeyConditionExpression": Key("PK").eq(k.eng_pk(engagement_id))
        & Key("SK").begins_with("DOC#"),
        "ProjectionExpression": "PK, SK, #t",
        "ExpressionAttributeNames": {"#t": "type"},
    }
    while True:
        r = repo.table.query(**kwargs)
        doomed += [i for i in r.get("Items", []) if i.get("type") == "FIELD"]
        lek = r.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek

    if apply and doomed:
        with repo.table.batch_writer() as batch:
            for item in doomed:
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
    return len(doomed)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--table", default="wheels-dev")
    ap.add_argument("--vehicles-table", default="wheels-dev-vehicles")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    repo = Repository(table_name=args.table, vehicles_table_name=args.vehicles_table)
    verb = "APPLY" if args.apply else "DRY RUN"
    print(f"[{verb}] {args.table}\n")

    if args.apply:
        programs, items = seed_from_file(repo)
        print(f"catalog: {programs} programs, {items} items\n")
    catalog = load_snapshot(repo, fresh=True)
    if not catalog.programs:
        sys.exit("the catalog is empty; run scripts/load_catalog.py --apply first")

    rng = random.Random(args.seed)
    service_programs = [p for p in PROGRAM_RATES if p not in LEASE_PROGRAMS]

    for engagement in sorted(repo.list_all_engagements(), key=lambda e: e.created_at):
        eid = engagement.engagement_id
        documents = repo.list_documents(eid)
        if not documents:
            print(f"  {engagement.client_name[:26]:<28} no documents, skipped")
            continue

        # A document that was really read keeps what was read from it. The test is whether
        # terms exist, not whether the version says "extracted": the seeder sets that status
        # without ever calling the extractor, so status alone would protect empty engagements
        # and leave them empty forever.
        existing = sum(
            len(repo.list_terms(eid, d.document_id, d.current_version)) for d in documents
        )
        if existing:
            print(f"  {engagement.client_name[:26]:<28} {existing} real terms, left alone")
            continue

        removed = drop_legacy_fields(repo, eid, args.apply)
        rng.shuffle(service_programs)
        made = 0
        for doc in documents:
            picked = (
                LEASE_PROGRAMS
                if doc.doc_type == "MLA"
                else service_programs[: rng.randrange(4, 8)]
            )
            build_terms(repo, eid, doc.document_id, rng, picked, catalog, args.apply)
            made += len(picked)
        covered = len({c.program_id for c in repo.list_coverage(eid)}) if args.apply else made
        print(
            f"  {engagement.client_name[:26]:<28} -{removed:>2} legacy rows  "
            f"+{len(documents)} documents  coverage {covered}"
        )

    if not args.apply:
        print("\n(dry run; pass --apply to write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
