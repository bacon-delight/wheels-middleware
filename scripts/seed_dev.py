"""Seed the dev environment with a realistic portfolio.

Creates customers, engagements across the lifecycle, a vehicle inventory of a few hundred
units, and — for the active engagements — extracted terms, a billing config, and a payment
schedule with a believable mix of paid, due and overdue rows. That mix is what makes the
finance dashboard show anything: collections, aging and concentration all come from it.

    python scripts/seed_dev.py --apply
"""

from __future__ import annotations

import argparse
import datetime
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3  # noqa: E402

from app.billing.config_builder import build_billing_config, ensure_schedule  # noqa: E402
from app.billing.estimate import compute_monthly_recurring  # noqa: E402
from app.catalog.store import load_snapshot, seed_from_file  # noqa: E402
from app.extraction.materialize import build_rows, coverage_rows  # noqa: E402
from app.lifecycle.submission_state import Role, SubmissionStatus  # noqa: E402
from app.objects import billing_config_key  # noqa: E402
from app.store.models import (  # noqa: E402
    AuditEvent,
    BodyClass,
    Customer,
    Document,
    DocumentVersion,
    Engagement,
    LeaseStructure,
    Membership,
    Powertrain,
    Submission,
    Vehicle,
    VehicleOwnership,
    VehicleStatus,
    required_doc_types,
)
from app.store.repository import Repository, new_id, utcnow  # noqa: E402
from app.store.s3 import S3Store  # noqa: E402
from app.validation.reconcile import reconcile_engagement  # noqa: E402

PROVIDER_ID = "340684c8-70c1-70f9-aa27-be7f0d41321e"
PROVIDER_EMAIL = "dipanjan.de@logiforma.com"
PROVIDER_NAME = "Dipanjan De"

# Customers mirror the synthetic contract set in Documents/Synthetic.
CUSTOMERS = [
    ("Apex Field Services LLC", "Field services", "Austin", "US"),
    ("Meridian Foods Inc", "Food distribution", "Chicago", "US"),
    ("Bluepine Utilities", "Utilities", "Denver", "US"),
    ("Norcap Logistics", "Logistics", "Newark", "US"),
    ("Stratus Pharma", "Pharmaceuticals", "Boston", "US"),
    ("Kestrel Construction", "Construction", "Phoenix", "US"),
    ("Omnia Retail Group", "Retail", "Atlanta", "US"),
    ("Halcyon Energy", "Energy", "Houston", "US"),
    ("Pinnacle Facilities", "Facilities management", "Seattle", "US"),
    ("Redoak Beverages", "Beverages", "Nashville", "US"),
    ("Solara Telecom", "Telecommunications", "San Jose", "US"),
    ("Veltrix Manufacturing", "Manufacturing", "Detroit", "US"),
]

# (customer index, name, scope, status, vehicles, months since billing start, term months,
#  days until the contract expires -- negative means it already lapsed)
ENGAGEMENTS = [
    (0, "Field fleet — 2024 renewal", "LEASE_AND_SERVICE", "ACTIVE", 48, 11, 36, 18),
    (0, "Q3 expansion — service vans", "LEASE_ONLY", "ACTIVE", 18, 5, 24, 540),
    (1, "Cold-chain distribution fleet", "LEASE_AND_SERVICE", "ACTIVE", 38, 9, 36, 47),
    (1, "Regional depot top-up", "LEASE_ONLY", "PENDING_CLIENT_APPROVAL", 14, None, 36, None),
    (2, "Managed services — owned fleet", "SERVICE_ONLY", "ACTIVE", 0, 7, 24, 130),
    (3, "Line-haul tractors", "LEASE_AND_SERVICE", "ACTIVE", 26, 13, 60, 1_180),
    (4, "Temperature-controlled vans", "LEASE_AND_SERVICE", "ACTIVE", 22, 4, 36, 78),
    (5, "Vocational trucks — West", "LEASE_ONLY", "ACTIVE", 31, 8, 48, 860),
    (6, "Store delivery fleet", "LEASE_AND_SERVICE", "ACTIVE", 42, 6, 36, -12),
    (7, "Field engineering pickups", "LEASE_ONLY", "IN_UNDERWRITING", 17, None, 36, None),
    (8, "Facilities vans", "LEASE_AND_SERVICE", "ACTIVE", 15, 3, 24, 205),
    (9, "Route delivery — Southeast", "LEASE_ONLY", "PENDING_FINANCE_APPROVAL", 24, None, 36, None),
    (10, "Technician fleet", "LEASE_AND_SERVICE", "ACTIVE", 28, 10, 36, 88),
    (11, "Plant logistics + forklifts", "LEASE_AND_SERVICE", "DRAFT", 0, None, 36, None),
]

# Synthetic terms are built against the real catalog rather than a private list, so seeded
# engagements resolve, cover and bill exactly the way extracted ones do. Rates are plausible
# per-vehicle-per-month figures; items are named the way the contracts name them.
PROGRAM_RATES: dict[str, tuple[str, float, str]] = {
    "maintenance-assistance": ("Monthly Program Fee", 18.50, "pvpm"),
    "fuel-management": ("Monthly Program Fee", 6.25, "pvpm"),
    "connected-vehicle": ("Telematics Subscription", 4.00, "pvpm"),
    "toll-management": ("Toll Management Fee", 2.75, "pvpm"),
    "violation-management": ("Violation Administration Fee", 1.50, "pvpm"),
    "insurance-card": ("Insurance Card Program Fee", 3.00, "per card"),
    "collision-management": ("Claim Loss Notice Fee", 15.00, "per notice"),
    "rental": ("Rental Administration Fee", 5.50, "per occurrence"),
    "registration-express": ("Registration Express Fee", 3.75, "pvpm"),
    "mileage-certification-logging": ("Mileage Certification Fee", 1.25, "per driver per month"),
    "remarketing": ("Remarketing Fee", 4.50, "per vehicle"),
    "safety-first-online-training": ("Online Safety Training", 2.00, "per driver per module"),
    "vehicle-lease": ("Lease Administrative Fee", 12.50, "pvpm"),
    "electric-vehicle": ("Home Charger Program Fee", 9.00, "pvpm"),
}

# Which programs belong to a lease agreement rather than a service one.
LEASE_PROGRAMS = ["vehicle-lease", "registration-express", "remarketing"]

# Fleet mix: roughly what a mixed corporate fleet looks like.
FLEET_MIX = [
    (BodyClass.SEDAN, 60), (BodyClass.SUV, 50), (BodyClass.PICKUP, 72),
    (BodyClass.CARGO_VAN, 66), (BodyClass.MINIVAN, 16),
    (BodyClass.BOX_TRUCK, 45), (BodyClass.SERVICE_BODY, 38), (BodyClass.STEP_VAN, 22),
    (BodyClass.STAKE_FLATBED, 16),
    (BodyClass.TRACTOR, 28), (BodyClass.VOCATIONAL_TRUCK, 18), (BodyClass.TRAILER, 20),
    (BodyClass.SPECIALTY_UPFIT, 12), (BodyClass.FORKLIFT, 15),
]
MAKES = {
    BodyClass.SEDAN: [("Toyota", "Camry"), ("Honda", "Accord"), ("Tesla", "Model 3")],
    BodyClass.SUV: [("Ford", "Explorer"), ("Chevrolet", "Tahoe"), ("Toyota", "RAV4")],
    BodyClass.PICKUP: [("Ford", "F-150"), ("Ram", "1500"), ("Chevrolet", "Silverado")],
    BodyClass.CARGO_VAN: [("Ford", "Transit"), ("Mercedes-Benz", "Sprinter"), ("Ram", "ProMaster")],
    BodyClass.MINIVAN: [("Chrysler", "Pacifica"), ("Toyota", "Sienna")],
    BodyClass.BOX_TRUCK: [("Isuzu", "NPR"), ("Hino", "195"), ("Freightliner", "M2")],
    BodyClass.SERVICE_BODY: [("Ford", "F-350"), ("Ram", "3500")],
    BodyClass.STEP_VAN: [("Freightliner", "MT45"), ("Morgan Olson", "Route Star")],
    BodyClass.STAKE_FLATBED: [("Ford", "F-550"), ("Isuzu", "FTR")],
    BodyClass.TRACTOR: [("Freightliner", "Cascadia"), ("Peterbilt", "579"), ("Volvo", "VNL")],
    BodyClass.VOCATIONAL_TRUCK: [("Mack", "Granite"), ("Kenworth", "T880")],
    BodyClass.TRAILER: [("Great Dane", "Everest"), ("Utility", "3000R")],
    BodyClass.SPECIALTY_UPFIT: [("Ford", "F-600 Utility"), ("Isuzu", "NRR Crane")],
    BodyClass.FORKLIFT: [("Toyota", "8FGU25"), ("Hyster", "H50XT")],
}
EV_CAPABLE = {BodyClass.SEDAN, BodyClass.SUV, BodyClass.CARGO_VAN, BodyClass.MINIVAN,
              BodyClass.STEP_VAN, BodyClass.FORKLIFT}


def build_vehicles(repo: Repository, rng: random.Random, apply: bool) -> list[Vehicle]:
    made, n = [], 0
    for body, count in FLEET_MIX:
        for _ in range(count):
            n += 1
            make, model = rng.choice(MAKES[body])
            powertrain = Powertrain.ICE.value
            if body in EV_CAPABLE:
                powertrain = rng.choices(
                    [Powertrain.ICE.value, Powertrain.HYBRID.value, Powertrain.BEV.value],
                    weights=[68, 18, 14],
                )[0]
            v = Vehicle(
                vehicle_id=new_id(),
                vin=f"1{rng.choice('FGH')}{rng.randrange(10**14, 10**15)}"[:17],
                unit_number=f"WH-{n:04d}",
                year=rng.choice([2022, 2023, 2023, 2024, 2024, 2025]),
                make=make, model=model, body_class=body.value, powertrain=powertrain,
                ownership=VehicleOwnership.WHEELS_OWNED.value,
                status=VehicleStatus.IN_STOCK.value,
                lease_structure=rng.choice([s.value for s in LeaseStructure]),
                lease_term_months=rng.choice([24, 36, 36, 48, 60]),
                odometer=rng.randrange(1_000, 90_000),
                created_by=PROVIDER_ID, created_at=utcnow(),
            )
            if apply:
                repo.put_vehicle(v)
            made.append(v)
    return made


def build_terms(repo: Repository, eid: str, did: str, rng: random.Random,
                program_ids: list[str], catalog, apply: bool) -> None:
    """Seed terms across all four categories, the way an extraction would produce them."""
    names = {p.program_id: p.name for p in catalog.programs}
    records: list[dict] = []
    for program_id in program_ids:
        item, rate, frequency = PROGRAM_RATES[program_id]
        program = names.get(program_id, program_id)
        records.append({
            "info_type": "pricing_item",
            "program": program, "item": item, "amount": rate, "frequency": frequency,
            "contract_section": "Pricing Schedule",
            "confidence": round(rng.uniform(0.86, 0.99), 2),
            "citations": [{
                "page": rng.randrange(3, 12),
                "section_label": f"Schedule {rng.randrange(1, 4)}",
                "quote": f"{item} {rate}",
            }],
        })
    # A contract is more than its prices; seeded engagements should look like it.
    if program_ids:
        lead = names.get(program_ids[0], program_ids[0])
        records += [
            {"info_type": "sla_item", "category": "Driver Contact Center (Answer)",
             "service_level_standard": "Average speed to answer under one minute.",
             "frequency": "Monthly", "minimum_threshold": "N/A",
             "contract_section": "Service Level Agreement", "confidence": 0.94,
             "citations": [{"page": 4, "quote": "average speed to answer"}]},
            {"info_type": "reporting_requirement", "report_name": "Downtime Report",
             "report_specifications": "Time between request and completion, per vehicle.",
             "frequency": "Monthly", "applicable_programs": [lead],
             "contract_section": "Report Requirements", "confidence": 0.92,
             "citations": [{"page": 6, "quote": "downtime report"}]},
            {"info_type": "definition", "term": "Vehicle",
             "definition": "Each vehicle leased or serviced under this agreement.",
             "contract_section": "Definitions", "confidence": 0.96,
             "citations": [{"page": 2, "quote": "means each vehicle"}]},
            {"info_type": "responsibility", "topic": "Invoicing",
             "task": "Vendor issues a consolidated monthly invoice.",
             "responsible_party": "Vendor", "party": "vendor", "frequency": "Monthly",
             "contract_section": "Fees, Payment and Invoicing", "confidence": 0.9,
             "citations": [{"page": 8, "quote": "consolidated monthly invoice"}]},
        ]

    rows, _ = build_rows(eid, did, 1, records, catalog=catalog, now=utcnow())
    for row in rows:
        row.approved = True
        row.needs_review = False
    if apply:
        repo.put_terms(rows)
        repo.clear_coverage(eid, did, 1)
        repo.put_coverage(coverage_rows(eid, did, 1, rows, catalog, now=utcnow()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="ap-south-2")
    ap.add_argument("--table", default="wheels-dev")
    ap.add_argument("--vehicles-table", default="wheels-dev-vehicles")
    ap.add_argument("--bucket", default="wheels-dev-docs-432417416277")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    apply = args.apply
    verb = "APPLY" if apply else "DRY RUN"
    ddb = boto3.resource("dynamodb", region_name=args.region)
    repo = Repository(table_name=args.table, resource=ddb,
                      vehicles_table_name=args.vehicles_table)
    s3 = S3Store(bucket=args.bucket)
    today = datetime.date.today()
    print(f"[{verb}] region={args.region}\n")

    # Terms are built against the real catalog, so the seeded engagements resolve and cover
    # exactly the way extracted ones do. Loading it first is what makes that possible.
    if apply:
        programs, items = seed_from_file(repo)
        print(f"  catalog: {programs} programs, {items} items")
    catalog = load_snapshot(repo, fresh=True)

    # --- customers ---
    cust_ids = []
    for name, industry, city, country in CUSTOMERS:
        c = Customer(customer_id=new_id(), legal_name=name, industry=industry, city=city,
                     country=country,
                     primary_contact_email=f"ap@{name.split()[0].lower()}.com",
                     created_by=PROVIDER_ID, created_at=utcnow())
        if apply:
            repo.put_customer(c)
        cust_ids.append(c.customer_id)
    print(f"customers: {len(cust_ids)}")

    # --- vehicle inventory ---
    vehicles = build_vehicles(repo, rng, apply)
    print(f"vehicles:  {len(vehicles)} Wheels-owned")

    pool = list(vehicles)
    rng.shuffle(pool)
    cursor = 0
    active_count = 0

    # A real fleet is never all in stock: some units are being serviced, some have aged out and
    # been remarketed, and some are still on order from the manufacturer.
    tail = pool[-72:]
    del pool[-72:]
    for i, v in enumerate(tail):
        status = (
            VehicleStatus.RETIRED.value if i < 30
            else VehicleStatus.IN_MAINTENANCE.value if i < 52
            else VehicleStatus.ON_ORDER.value
        )
        if apply:
            repo.update_vehicle(v.vehicle_id, {"status": status})
    print("           30 retired, 22 in maintenance, 20 on order")

    for ci, name, scope, status, want_vehicles, months, term, expires_in in ENGAGEMENTS:
        eid, sid = new_id(), new_id()
        customer_id = cust_ids[ci]
        legal = CUSTOMERS[ci][0]
        eng = Engagement(
            engagement_id=eid, name=name, client_name=legal, customer_id=customer_id,
            scope=None, status=status, fleet_size=0,
            created_by=PROVIDER_ID,
            created_at=(today - datetime.timedelta(days=rng.randrange(60, 420))).isoformat(),
        )
        if apply:
            repo.put_engagement(eng)
            if expires_in is not None:
                end = today + datetime.timedelta(days=expires_in)
                repo.set_engagement_contract(
                    eid, (end - datetime.timedelta(days=30 * term)).isoformat(), term,
                    end.isoformat(), rng.random() < 0.4, rng.choice([60, 90, 90, 120]),
                )
            repo.put_membership(Membership(
                engagement_id=eid, user_id=PROVIDER_ID, email=PROVIDER_EMAIL,
                role=Role.PROVIDER, name=PROVIDER_NAME, created_at=utcnow(),
            ))
            repo.put_audit(AuditEvent(
                engagement_id=eid, event_id=new_id(), ts=utcnow(), actor_id=PROVIDER_ID,
                actor_role="provider", actor_name=PROVIDER_NAME, action="engagement_created",
            ))

        # --- documents: the agreements in force, plus the odd superseded one on file ---
        doc_ids = {}
        for doc_type in required_doc_types(scope):
            did = new_id()
            doc_ids[doc_type] = did
            if apply:
                effective = (today - datetime.timedelta(days=30 * (months or 6))).isoformat()
                repo.put_document(Document(
                    engagement_id=eid, document_id=did, doc_type=doc_type,
                    filename=f"{legal.split()[0]}_{doc_type}_{effective[:4]}.pdf",
                    current_version=1, standing="CURRENT", effective_date=effective,
                    classified_type=doc_type, classification_confidence=0.99,
                    created_at=utcnow(),
                ))
                # About a third of customers also hand over the agreement this one replaced.
                if rng.random() < 0.35:
                    prior_id = new_id()
                    prior_eff = (
                        datetime.date.fromisoformat(effective)
                        - datetime.timedelta(days=365 * rng.choice([2, 3]))
                    ).isoformat()
                    repo.put_document(Document(
                        engagement_id=eid, document_id=prior_id, doc_type=doc_type,
                        filename=f"{legal.split()[0]}_{doc_type}_{prior_eff[:4]}.pdf",
                        current_version=1, standing="SUPERSEDED", effective_date=prior_eff,
                        classified_type=doc_type, classification_confidence=0.99,
                        created_at=utcnow(),
                    ))
                    repo.put_document_version(DocumentVersion(
                        engagement_id=eid, document_id=prior_id, version=1,
                        s3_key=f"{eid}/{prior_id}/v0001.pdf", page_count=rng.randrange(9, 22),
                        status="extracted", uploaded_at=utcnow(),
                    ))
                repo.put_document_version(DocumentVersion(
                    engagement_id=eid, document_id=did, version=1,
                    s3_key=f"{eid}/{did}/v0001.pdf", page_count=rng.randrange(9, 22),
                    status="extracted", uploaded_at=utcnow(),
                ))

        sub = Submission(
            engagement_id=eid, submission_id=sid, status=SubmissionStatus(status),
            document_ids=doc_ids,
            msa_document_id=doc_ids.get("MSA"), mla_document_id=doc_ids.get("MLA"),
            created_at=utcnow(), updated_at=utcnow(),
        )
        if apply:
            repo.put_submission(sub)
            # Settle standing, scope and the submission's slots the way an upload would.
            reconcile_engagement(repo, eid)

        # --- terms: the MSA carries the service lines, the MLA the lease-side ones ---
        if status != "DRAFT":
            service_programs = [p for p in PROGRAM_RATES if p not in LEASE_PROGRAMS]
            rng.shuffle(service_programs)
            for doc_type, did in doc_ids.items():
                picked = (
                    LEASE_PROGRAMS if doc_type == "MLA"
                    else service_programs[: rng.randrange(4, 8)]
                )
                build_terms(repo, eid, did, rng, picked, catalog, apply)

        # --- vehicles onto the engagement ---
        assigned = 0
        if want_vehicles and apply:
            take = pool[cursor:cursor + want_vehicles]
            cursor += want_vehicles
            for v in take:
                repo.assign_vehicle(v.vehicle_id, eid, customer_id, PROVIDER_ID)
            assigned = len(take)
        elif want_vehicles:
            assigned = want_vehicles
            cursor += want_vehicles

        # Customer-owned units for the service-only engagement: Wheels services, never leases.
        if scope == "SERVICE_ONLY":
            for i in range(26):
                v = Vehicle(
                    vehicle_id=new_id(), unit_number=f"{legal.split()[0][:3].upper()}-{i + 1:03d}",
                    year=rng.choice([2019, 2020, 2021, 2022]),
                    make=rng.choice(["Ford", "Isuzu", "Chevrolet"]),
                    model=rng.choice(["F-250", "NPR", "Express"]),
                    body_class=rng.choice([BodyClass.PICKUP.value, BodyClass.BOX_TRUCK.value]),
                    ownership=VehicleOwnership.CUSTOMER_OWNED.value, customer_id=customer_id,
                    status=VehicleStatus.ON_ORDER.value,
                    created_by=PROVIDER_ID, created_at=utcnow(),
                )
                if apply:
                    repo.put_vehicle(v)
                    repo.assign_vehicle(v.vehicle_id, eid, customer_id, PROVIDER_ID)
            assigned = 26

        # --- billing for the active book ---
        if status == "ACTIVE" and apply:
            fleet = assigned or 1
            config = build_billing_config(repo, eid, sid, s3=s3)
            monthly = compute_monthly_recurring(config, fleet)
            repo.set_engagement_billing(eid, fleet, monthly)
            s3.put_bytes(billing_config_key(eid, sid),
                         json.dumps(config).encode(), content_type="application/json")
            # `_billing_start` anchors on the engagement's own billing_start when it is set,
            # and otherwise on the config's generation time — which is now. Set it first so the
            # schedule reaches back and the dashboard has real payment history to show.
            from app.billing.schedule import normalize_frequency, period_multiplier

            frequency = normalize_frequency(config)
            mult = period_multiplier(frequency)
            start = today - datetime.timedelta(days=30 * (months or 6))
            repo.set_engagement_schedule(eid, start.isoformat(), frequency)
            engagement = repo.get_engagement(eid)
            payments = ensure_schedule(repo, engagement, sid, config, today=start)
            # Pay the rows that have come due, leaving a couple of engagements behind so the
            # aging buckets and the at-risk list have something in them.
            # A third of the book runs late: the oldest dues are settled, the most recent few
            # are not, which is what puts rows into the 1-30 / 31-60 / 60+ aging buckets.
            behind = rng.random() < 0.35
            unpaid_tail = rng.choice([2, 3, 4]) if behind else 0
            due_rows = [p for p in payments if datetime.date.fromisoformat(p.due_date) <= today]
            skip = {p.seq for p in due_rows[-unpaid_tail:]} if unpaid_tail else set()
            for p in payments:
                due = datetime.date.fromisoformat(p.due_date)
                if due > today or p.seq in skip:
                    continue
                p.paid = True
                p.paid_at = utcnow()
                p.paid_by = PROVIDER_NAME
                p.amount_paid = round(monthly * mult, 2)
                repo.put_payment(p)
            active_count += 1
            print(f"  ACTIVE  {name[:34]:34s} {fleet:3d} vehicles  ${monthly:>10,.2f}/mo"
                  f"{'   (behind)' if behind else ''}")
        else:
            if apply and assigned:
                repo.set_engagement_billing(eid, assigned, None)
            print(f"  {status:11s} {name[:34]:34s} {assigned:3d} vehicles")

    print(f"\nengagements: {len(ENGAGEMENTS)} ({active_count} active)")
    print(f"vehicles assigned: {cursor}, remaining in stock: {len(vehicles) - cursor}")
    if not apply:
        print("\nnothing written — re-run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
