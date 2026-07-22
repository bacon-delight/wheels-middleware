"""Golden dataset — the ground-truth billing terms from the brief (section 4).

Two artifacts:
  1. `golden_extraction(filename)` builds a full `ContractExtraction` for each sample
     contract. These prove the schema can express every pricing shape (flat, per-unit,
     per-transaction, cost-plus, tiered/marginal, conditional waiver, rebate share,
     minimum, escalator, election, TRAC split, per-class depreciation) and document the
     ground truth in code.
  2. `EXPECTATIONS[filename]` is a list of (description, predicate) checks — the tolerant
     eval target used by the live-extraction test. Each golden object satisfies its own
     expectations (a self-consistency test that runs with no LLM).

Client A — Meridian Foods Corporation (flat/simple).  Client B — Apex Field Services LLC
(tiered/conditional traps, Rentals NOT elected).
"""

from __future__ import annotations

from collections.abc import Callable

from app.extraction.schema import (
    ALL_SERVICE_LINES,
    BBoxModel,  # noqa: F401  (re-exported for convenience)
    Citation,
    Condition,
    ConditionType,
    ContractExtraction,
    DepreciationRate,
    DocType,
    EarlyTermination,
    Escalator,
    EscalatorType,
    FeeItem,
    FeeType,
    LeaseRate,
    LeaseTerms,
    RateBasis,
    ServiceLine,
    ServiceLineTerms,
    TierBand,
    UnitBasis,
)

TOL = 0.02

# --------------------------------------------------------------------------------------
# Accessors used by expectation predicates (operate on a ContractExtraction).
# Note: schema uses use_enum_values=True, so .service / .fee_type / condition .type are strings.
# --------------------------------------------------------------------------------------


def approx(a: float | None, b: float, tol: float = TOL) -> bool:
    return a is not None and abs(a - b) <= tol


def _line(ex: ContractExtraction, service: ServiceLine) -> ServiceLineTerms | None:
    return ex.line(service)


def elected(ex: ContractExtraction, service: ServiceLine) -> bool:
    sl = _line(ex, service)
    return bool(sl and sl.elected)


def fee_amounts(ex: ContractExtraction, service: ServiceLine) -> list[float]:
    sl = _line(ex, service)
    if not sl:
        return []
    out = [fi.amount for fi in sl.fee_items if fi.amount is not None]
    out += [fi.minimum for fi in sl.fee_items if fi.minimum is not None]
    return out


def fee_rates(ex: ContractExtraction, service: ServiceLine) -> list[float]:
    sl = _line(ex, service)
    if not sl:
        return []
    return [fi.rate_pct for fi in sl.fee_items if fi.rate_pct is not None]


def tier_amounts(ex: ContractExtraction, service: ServiceLine) -> list[float]:
    sl = _line(ex, service)
    if not sl:
        return []
    return [tb.amount for fi in sl.fee_items for tb in fi.tier_bands if tb.amount is not None]


def any_tier_amount(ex: ContractExtraction, val: float) -> bool:
    """A tier band of this amount anywhere: a service line OR a cross-service bundled fee."""
    amounts: list[float] = []
    for sl in ex.service_lines:
        amounts += [tb.amount for fi in sl.fee_items for tb in fi.tier_bands if tb.amount is not None]
    for bf in ex.bundled_fees:
        amounts += [tb.amount for fi in bf.fee_items for tb in fi.tier_bands if tb.amount is not None]
    return any(approx(a, val) for a in amounts)


def has_waiver_mentioning(ex: ContractExtraction, service: ServiceLine, keyword: str) -> bool:
    """A waiver condition on this service whose trigger OR description mentions keyword."""
    sl = _line(ex, service)
    if not sl:
        return False
    kw = keyword.lower()
    for fi in sl.fee_items:
        for c in fi.conditions:
            if c.type != ConditionType.WAIVER.value:
                continue
            if kw in (c.trigger or "").lower() or kw in (c.description or "").lower():
                return True
    return False


def has_amount(ex: ContractExtraction, service: ServiceLine, val: float) -> bool:
    return any(approx(a, val) for a in fee_amounts(ex, service))


def has_rate(ex: ContractExtraction, service: ServiceLine, val: float) -> bool:
    return any(approx(r, val) for r in fee_rates(ex, service))


def has_tier_amount(ex: ContractExtraction, service: ServiceLine, val: float) -> bool:
    return any(approx(a, val) for a in tier_amounts(ex, service))


def has_trigger(ex: ContractExtraction, service: ServiceLine, trigger: str) -> bool:
    sl = _line(ex, service)
    if not sl:
        return False
    return any(
        (c.trigger or "").lower() == trigger.lower()
        for fi in sl.fee_items
        for c in fi.conditions
    )


def has_condition_type(ex: ContractExtraction, service: ServiceLine, ctype: str) -> bool:
    sl = _line(ex, service)
    if not sl:
        return False
    return any(c.type == ctype for fi in sl.fee_items for c in fi.conditions)


def depreciation_pct(ex: ContractExtraction, class_substr: str) -> float | None:
    if not ex.lease_terms:
        return None
    for d in ex.lease_terms.depreciation:
        if class_substr.lower() in d.vehicle_class.lower():
            return d.monthly_pct
    return None


Predicate = Callable[[ContractExtraction], bool]
Expectation = tuple[str, Predicate]


# --------------------------------------------------------------------------------------
# EXPECTATIONS — tolerant checks per document (the live-eval target).
# --------------------------------------------------------------------------------------

EXPECTATIONS: dict[str, list[Expectation]] = {
    "MSA_MeridianFoods.pdf": [
        ("doc_type MSA", lambda e: e.doc_type == DocType.MSA.value),
        ("client is Meridian", lambda e: "meridian" in e.client_name.lower()),
        ("all 13 services elected", lambda e: all(elected(e, s) for s in ALL_SERVICE_LINES)),
        ("fuel card $4.00", lambda e: has_amount(e, ServiceLine.FUEL, 4.00)),
        ("fuel txn $0.95", lambda e: has_amount(e, ServiceLine.FUEL, 0.95)),
        ("fuel out-of-network 2.5%", lambda e: has_rate(e, ServiceLine.FUEL, 2.5)),
        ("maintenance $11.50", lambda e: has_amount(e, ServiceLine.MAINTENANCE, 11.50)),
        ("violation $13.50", lambda e: has_amount(e, ServiceLine.VIOLATION, 13.50)),
        ("violation escalated $25", lambda e: has_amount(e, ServiceLine.VIOLATION, 25.0)),
        ("collision $145/claim", lambda e: has_amount(e, ServiceLine.COLLISION, 145.0)),
        ("collision 15% subrogation", lambda e: has_rate(e, ServiceLine.COLLISION, 15.0)),
        ("telematics $99 hardware", lambda e: has_amount(e, ServiceLine.TELEMATICS, 99.0)),
        ("telematics $17.95/unit/mo", lambda e: has_amount(e, ServiceLine.TELEMATICS, 17.95)),
        ("remarketing $195/unit", lambda e: has_amount(e, ServiceLine.REMARKETING, 195.0)),
        ("remarketing 2% proceeds", lambda e: has_rate(e, ServiceLine.REMARKETING, 2.0)),
        ("MVR $6.50", lambda e: has_amount(e, ServiceLine.DRIVER_BACKGROUND_SAFETY, 6.50)),
        (
            "CPI-capped 3% escalator",
            lambda e: e.escalator is not None
            and e.escalator.type == EscalatorType.CPI_CAPPED.value
            and approx(e.escalator.pct, 3.0),
        ),
    ],
    "MLA_MeridianFoods.pdf": [
        ("doc_type MLA", lambda e: e.doc_type == DocType.MLA.value),
        ("client is Meridian", lambda e: "meridian" in e.client_name.lower()),
        ("has lease terms", lambda e: e.lease_terms is not None),
        (
            "floating SOFR + 235bps",
            lambda e: e.lease_terms is not None
            and e.lease_terms.rate.basis == RateBasis.FLOATING.value
            and e.lease_terms.rate.spread_bps == 235,
        ),
        ("sedan depreciation 2.00%", lambda e: approx(depreciation_pct(e, "sedan"), 2.00)),
        ("class 4-6 depreciation 1.60%", lambda e: approx(depreciation_pct(e, "class"), 1.60)),
        (
            "admin fee $12.50/unit/mo",
            lambda e: e.lease_terms is not None
            and e.lease_terms.admin_fee is not None
            and approx(e.lease_terms.admin_fee.amount, 12.50),
        ),
        (
            "early termination book value + $350",
            lambda e: e.lease_terms is not None
            and e.lease_terms.early_termination is not None
            and "350" in e.lease_terms.early_termination.formula,
        ),
        (
            "TRAC surplus 100% to lessee",
            lambda e: e.lease_terms is not None
            and "100" in (e.lease_terms.trac_surplus_split or ""),
        ),
    ],
    "MSA_ApexFieldServices.pdf": [
        ("doc_type MSA", lambda e: e.doc_type == DocType.MSA.value),
        ("client is Apex", lambda e: "apex" in e.client_name.lower()),
        ("RENTALS NOT elected (the trap)", lambda e: not elected(e, ServiceLine.RENTALS)),
        # Bundled management-fee tiers may live under a service line OR in bundled_fees.
        ("tier $21.50 (units 1-250)", lambda e: any_tier_amount(e, 21.50)),
        ("tier $18.75 (units 251-500)", lambda e: any_tier_amount(e, 18.75)),
        ("tier $16.25 (units 501+)", lambda e: any_tier_amount(e, 16.25)),
        ("fuel cost + 1.75%", lambda e: has_rate(e, ServiceLine.FUEL, 1.75)),
        (
            "fuel 50% rebate share above $12,500/qtr",
            lambda e: any(
                (c.type == ConditionType.REBATE_SHARE.value)
                and approx(c.share_pct, 50.0)
                and approx(c.threshold_amount, 12500.0)
                for sl in [_line(e, ServiceLine.FUEL)]
                if sl
                for fi in sl.fee_items
                for c in fi.conditions
            ),
        ),
        ("title/reg cost + 12%", lambda e: has_rate(e, ServiceLine.INITIAL_TITLE_REG, 12.0)),
        ("maintenance repairs +6% network", lambda e: has_rate(e, ServiceLine.MAINTENANCE, 6.0)),
        ("maintenance repairs +10% non-network", lambda e: has_rate(e, ServiceLine.MAINTENANCE, 10.0)),
        ("violation $18 first 50", lambda e: has_tier_amount(e, ServiceLine.VIOLATION, 18.0)),
        ("violation $12 thereafter", lambda e: has_tier_amount(e, ServiceLine.VIOLATION, 12.0)),
        ("collision cost + 8%", lambda e: has_rate(e, ServiceLine.COLLISION, 8.0)),
        ("collision $95 intake fee", lambda e: has_amount(e, ServiceLine.COLLISION, 95.0)),
        (
            "collision intake waived if telematics-enrolled",
            lambda e: has_waiver_mentioning(e, ServiceLine.COLLISION, "telematics"),
        ),
        ("remarketing 3.5% proceeds", lambda e: has_rate(e, ServiceLine.REMARKETING, 3.5)),
        ("remarketing min $250", lambda e: has_amount(e, ServiceLine.REMARKETING, 250.0)),
        (
            "fixed 2.5% escalator",
            lambda e: e.escalator is not None and approx(e.escalator.pct, 2.5),
        ),
        ("Net 45 terms", lambda e: "45" in (e.payment_terms or "")),
    ],
    "MLA_ApexFieldServices.pdf": [
        ("doc_type MLA", lambda e: e.doc_type == DocType.MLA.value),
        ("client is Apex", lambda e: "apex" in e.client_name.lower()),
        (
            "fixed 6.45% p.a. lease charge",
            lambda e: e.lease_terms is not None
            and e.lease_terms.rate.basis == RateBasis.FIXED.value
            and approx(e.lease_terms.rate.fixed_rate_pct, 6.45),
        ),
        ("sedan depreciation 2.25%", lambda e: approx(depreciation_pct(e, "sedan"), 2.25)),
        ("pickup depreciation 2.08%", lambda e: approx(depreciation_pct(e, "pickup"), 2.08)),
        ("van depreciation 1.90%", lambda e: approx(depreciation_pct(e, "van"), 1.90)),
        (
            "admin fee $9.95/unit/mo",
            lambda e: e.lease_terms is not None
            and e.lease_terms.admin_fee is not None
            and approx(e.lease_terms.admin_fee.amount, 9.95),
        ),
        (
            "early termination book value + 3 months' depreciation",
            lambda e: e.lease_terms is not None
            and e.lease_terms.early_termination is not None
            and "3" in e.lease_terms.early_termination.formula,
        ),
        (
            "TRAC surplus first $2,500 to client, 90/10 above",
            lambda e: e.lease_terms is not None
            and "2,500" in (e.lease_terms.trac_surplus_split or "")
            or "2500" in (e.lease_terms.trac_surplus_split or ""),
        ),
    ],
}


def check(ex: ContractExtraction, filename: str) -> list[str]:
    """Return the descriptions of every failed expectation for `filename`."""
    return [desc for desc, pred in EXPECTATIONS[filename] if not pred(ex)]


# --------------------------------------------------------------------------------------
# Golden ContractExtraction builders (schema-expressiveness proof + self-consistency).
# --------------------------------------------------------------------------------------


def _elected_all_except(*not_elected: ServiceLine) -> dict[ServiceLine, bool]:
    return {s: (s not in not_elected) for s in ALL_SERVICE_LINES}


def _sl(
    service: ServiceLine,
    elected_: bool,
    fee_items: list[FeeItem] | None = None,
    conf: float = 0.95,
    page: int = 5,
) -> ServiceLineTerms:
    return ServiceLineTerms(
        service=service,
        elected=elected_,
        fee_items=fee_items or [],
        confidence=conf,
        citations=[Citation(page=page, quote=f"{service.value} fee schedule")] if elected_ else [],
    )


def _meridian_msa() -> ContractExtraction:
    lines: list[ServiceLineTerms] = []
    fees = {
        ServiceLine.FUEL: [
            FeeItem(description="Fuel card monthly fee", fee_type=FeeType.FLAT, amount=4.00,
                    unit_basis=UnitBasis.PER_CARD_PER_MONTH),
            FeeItem(description="Per-transaction fee", fee_type=FeeType.PER_TRANSACTION, amount=0.95,
                    unit_basis=UnitBasis.PER_TRANSACTION),
            FeeItem(description="Out-of-network surcharge", fee_type=FeeType.PERCENTAGE, rate_pct=2.5,
                    conditions=[Condition(type=ConditionType.SURCHARGE, description="Out-of-network",
                                          trigger="out_of_network")]),
        ],
        ServiceLine.MAINTENANCE: [
            FeeItem(description="Maintenance management", fee_type=FeeType.FLAT, amount=11.50,
                    unit_basis=UnitBasis.PER_VEHICLE_PER_MONTH)
        ],
        ServiceLine.VIOLATION: [
            FeeItem(description="Violation processing", fee_type=FeeType.FLAT, amount=13.50,
                    unit_basis=UnitBasis.PER_INCIDENT),
            FeeItem(description="Escalated violation", fee_type=FeeType.FLAT, amount=25.0,
                    unit_basis=UnitBasis.PER_INCIDENT),
        ],
        ServiceLine.COLLISION: [
            FeeItem(description="Collision claim handling", fee_type=FeeType.FLAT, amount=145.0,
                    unit_basis=UnitBasis.PER_CLAIM),
            FeeItem(description="Subrogation retention", fee_type=FeeType.PERCENTAGE, rate_pct=15.0,
                    conditions=[Condition(type=ConditionType.THRESHOLD, description="Subrogation retention")]),
        ],
        ServiceLine.TELEMATICS: [
            FeeItem(description="Telematics hardware", fee_type=FeeType.FLAT, amount=99.0,
                    unit_basis=UnitBasis.ONE_TIME),
            FeeItem(description="Telematics subscription", fee_type=FeeType.FLAT, amount=17.95,
                    unit_basis=UnitBasis.PER_UNIT_PER_MONTH),
        ],
        ServiceLine.REMARKETING: [
            FeeItem(description="Remarketing per unit", fee_type=FeeType.FLAT, amount=195.0,
                    unit_basis=UnitBasis.PER_UNIT),
            FeeItem(description="Gross proceeds share", fee_type=FeeType.PERCENTAGE, rate_pct=2.0,
                    unit_basis=UnitBasis.PERCENT_OF_PROCEEDS),
        ],
        ServiceLine.DRIVER_BACKGROUND_SAFETY: [
            FeeItem(description="Motor vehicle record (MVR)", fee_type=FeeType.PER_TRANSACTION,
                    amount=6.50, unit_basis=UnitBasis.PER_TRANSACTION)
        ],
    }
    for s in ALL_SERVICE_LINES:
        lines.append(_sl(s, True, fees.get(s)))
    return ContractExtraction(
        doc_type=DocType.MSA,
        client_name="Meridian Foods Corporation",
        payment_terms="Net 30",
        escalator=Escalator(type=EscalatorType.CPI_CAPPED, pct=3.0, cap_pct=3.0),
        service_lines=lines,
        confidence=0.95,
    )


def _meridian_mla() -> ContractExtraction:
    return ContractExtraction(
        doc_type=DocType.MLA,
        client_name="Meridian Foods Corporation",
        lease_terms=LeaseTerms(
            lease_type="open_end_TRAC",
            rate=LeaseRate(basis=RateBasis.FLOATING, index="SOFR_30d_avg", spread_bps=235),
            depreciation=[
                DepreciationRate(vehicle_class="sedan", monthly_pct=2.00),
                DepreciationRate(vehicle_class="light truck", monthly_pct=1.85),
                DepreciationRate(vehicle_class="class 4-6", monthly_pct=1.60),
            ],
            admin_fee=FeeItem(description="Lease admin fee", fee_type=FeeType.FLAT, amount=12.50,
                              unit_basis=UnitBasis.PER_UNIT_PER_MONTH),
            early_termination=EarlyTermination(formula="book value + $350",
                                               components=["book_value", "flat_350"]),
            trac_surplus_split="100% to lessee",
            billing_frequency="monthly",
            confidence=0.95,
            citations=[Citation(page=11, section_label="Schedule B",
                                quote="Delivery and freight: $465.00")],
        ),
        confidence=0.95,
    )


def _apex_msa() -> ContractExtraction:
    lines: list[ServiceLineTerms] = []
    fees = {
        ServiceLine.MAINTENANCE: [
            FeeItem(description="Bundled management fee (maintenance + insurance + mileage)",
                    fee_type=FeeType.TIERED, unit_basis=UnitBasis.PER_VEHICLE_PER_MONTH,
                    tier_bands=[
                        TierBand(min_units=1, max_units=250, amount=21.50, marginal=True),
                        TierBand(min_units=251, max_units=500, amount=18.75, marginal=True),
                        TierBand(min_units=501, max_units=None, amount=16.25, marginal=True),
                    ]),
            FeeItem(description="Maintenance repairs (network)", fee_type=FeeType.COST_PLUS,
                    rate_pct=6.0, unit_basis=UnitBasis.PERCENT_OF_COST,
                    conditions=[Condition(type=ConditionType.THRESHOLD, description="Network shop",
                                          trigger="network")]),
            FeeItem(description="Maintenance repairs (non-network)", fee_type=FeeType.COST_PLUS,
                    rate_pct=10.0, unit_basis=UnitBasis.PERCENT_OF_COST,
                    conditions=[Condition(type=ConditionType.THRESHOLD, description="Non-network shop",
                                          trigger="non_network")]),
        ],
        ServiceLine.FUEL: [
            FeeItem(description="Fuel at cost plus markup", fee_type=FeeType.COST_PLUS, rate_pct=1.75,
                    unit_basis=UnitBasis.PERCENT_OF_COST,
                    conditions=[Condition(type=ConditionType.REBATE_SHARE,
                                          description="50% rebate share above $12,500/quarter",
                                          trigger="quarterly_volume", share_pct=50.0,
                                          threshold_amount=12500.0)]),
        ],
        ServiceLine.INITIAL_TITLE_REG: [
            FeeItem(description="Title & registration at cost plus", fee_type=FeeType.COST_PLUS,
                    rate_pct=12.0, unit_basis=UnitBasis.PERCENT_OF_COST),
        ],
        ServiceLine.VIOLATION: [
            FeeItem(description="Violation processing (volume tiered)", fee_type=FeeType.TIERED,
                    unit_basis=UnitBasis.PER_INCIDENT,
                    tier_bands=[
                        TierBand(min_units=1, max_units=50, amount=18.0, marginal=True),
                        TierBand(min_units=51, max_units=None, amount=12.0, marginal=True),
                    ]),
        ],
        ServiceLine.COLLISION: [
            FeeItem(description="Collision at cost plus", fee_type=FeeType.COST_PLUS, rate_pct=8.0,
                    unit_basis=UnitBasis.PERCENT_OF_COST),
            FeeItem(description="Intake fee", fee_type=FeeType.CONDITIONAL, amount=95.0,
                    unit_basis=UnitBasis.PER_CLAIM,
                    conditions=[Condition(type=ConditionType.WAIVER,
                                          description="Waived if vehicle is telematics-enrolled",
                                          trigger="telematics_enrolled")]),
        ],
        ServiceLine.REMARKETING: [
            FeeItem(description="Remarketing percentage of proceeds", fee_type=FeeType.PERCENTAGE,
                    rate_pct=3.5, unit_basis=UnitBasis.PERCENT_OF_PROCEEDS, minimum=250.0),
        ],
    }
    election = _elected_all_except(ServiceLine.RENTALS)
    for s in ALL_SERVICE_LINES:
        lines.append(_sl(s, election[s], fees.get(s)))
    return ContractExtraction(
        doc_type=DocType.MSA,
        client_name="Apex Field Services LLC",
        payment_terms="Net 45",
        true_up="quarterly",
        escalator=Escalator(type=EscalatorType.FIXED, pct=2.5),
        service_lines=lines,
        confidence=0.93,
    )


def _apex_mla() -> ContractExtraction:
    return ContractExtraction(
        doc_type=DocType.MLA,
        client_name="Apex Field Services LLC",
        lease_terms=LeaseTerms(
            lease_type="open_end_TRAC",
            rate=LeaseRate(basis=RateBasis.FIXED, fixed_rate_pct=6.45),
            depreciation=[
                DepreciationRate(vehicle_class="sedan", monthly_pct=2.25),
                DepreciationRate(vehicle_class="pickup", monthly_pct=2.08),
                DepreciationRate(vehicle_class="van", monthly_pct=1.90),
                DepreciationRate(vehicle_class="class 5-6", monthly_pct=1.67),
            ],
            admin_fee=FeeItem(description="Lease admin fee", fee_type=FeeType.FLAT, amount=9.95,
                              unit_basis=UnitBasis.PER_UNIT_PER_MONTH),
            early_termination=EarlyTermination(
                formula="book value + 3 months' depreciation",
                components=["book_value", "3_months_depreciation"]),
            trac_surplus_split="first $2,500 to client, 90/10 above",
            billing_frequency="quarterly_in_advance",
            confidence=0.92,
            citations=[Citation(page=11, section_label="Schedule B",
                                quote="Delivery and freight: $610.00")],
        ),
        confidence=0.92,
    )


_BUILDERS = {
    "MSA_MeridianFoods.pdf": _meridian_msa,
    "MLA_MeridianFoods.pdf": _meridian_mla,
    "MSA_ApexFieldServices.pdf": _apex_msa,
    "MLA_ApexFieldServices.pdf": _apex_mla,
}


def golden_extraction(filename: str) -> ContractExtraction:
    return _BUILDERS[filename]()


ALL_DOCS = tuple(_BUILDERS.keys())
