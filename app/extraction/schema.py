"""The extraction contract.

These Pydantic models are the single source of truth for (a) the LLM tool-use schema,
(b) validation of model output, and (c) the shape stored in DynamoDB / shown in the
review UI. The schema must express every pricing shape found in the sample contracts:
flat fees, per-unit / per-transaction fees, percentage & cost-plus markups, volume tier
bands (marginal or whole-fleet), conditional waivers/surcharges, rebate shares,
escalators, minimums, service elections, and MLA lease mechanics (floating/fixed rate,
per-class depreciation, TRAC surplus splits, early-termination formulas).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _coerce_enum(value, enum_cls, default):
    """Map model output onto an enum, coercing unknown values to a safe default instead of
    failing the whole extraction. Unknown values get flagged for review downstream."""
    if value is None:
        return value
    if isinstance(value, enum_cls):
        return value.value
    s = str(value)
    for member in enum_cls:
        if member.value == s or member.name == s:
            return member.value
    return default.value


class ServiceLine(str, Enum):
    FUEL = "Fuel"
    INITIAL_TITLE_REG = "InitialTitleReg"
    MAINTENANCE = "Maintenance"
    REG_RENEWALS = "RegRenewals"
    TOLL = "Toll"
    VIOLATION = "Violation"
    COLLISION = "Collision"
    INSURANCE = "Insurance"
    MILEAGE_LOGGING = "MileageLogging"
    TELEMATICS = "Telematics"
    RENTALS = "Rentals"
    DRIVER_BACKGROUND_SAFETY = "DriverBackgroundSafety"
    REMARKETING = "Remarketing"


ALL_SERVICE_LINES: tuple[ServiceLine, ...] = tuple(ServiceLine)


class DocType(str, Enum):
    MSA = "MSA"
    MLA = "MLA"


class FeeType(str, Enum):
    FLAT = "flat"
    PER_UNIT = "per_unit"
    PER_TRANSACTION = "per_transaction"
    PERCENTAGE = "percentage"
    COST_PLUS = "cost_plus"
    TIERED = "tiered"
    MINIMUM = "minimum"
    CONDITIONAL = "conditional"
    OTHER = "other"


class UnitBasis(str, Enum):
    PER_CARD_PER_MONTH = "per_card_per_month"
    PER_VEHICLE_PER_MONTH = "per_vehicle_per_month"
    PER_UNIT_PER_MONTH = "per_unit_per_month"
    PER_TRANSACTION = "per_transaction"
    PER_CLAIM = "per_claim"
    PER_UNIT = "per_unit"
    PER_INCIDENT = "per_incident"
    PERCENT_OF_PROCEEDS = "percent_of_proceeds"
    PERCENT_OF_COST = "percent_of_cost"
    ONE_TIME = "one_time"
    OTHER = "other"


class ConditionType(str, Enum):
    WAIVER = "waiver"
    SURCHARGE = "surcharge"
    THRESHOLD = "threshold"
    REBATE_SHARE = "rebate_share"
    MINIMUM = "minimum"
    OTHER = "other"


class EscalatorType(str, Enum):
    FIXED = "fixed"
    CPI_CAPPED = "CPI_capped"
    NONE = "none"


class RateBasis(str, Enum):
    FLOATING = "floating"
    FIXED = "fixed"


class BBoxModel(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class Citation(BaseModel):
    """Where a value came from. `bbox` is resolved server-side from `quote`, not the LLM."""

    model_config = ConfigDict(use_enum_values=True)

    page: int = Field(ge=1)
    section_label: str | None = None
    quote: str = Field(description="Verbatim text copied from the contract page.")
    char_span: tuple[int, int] | None = None
    bbox: BBoxModel | None = None
    engine: str | None = None


class Escalator(BaseModel):
    model_config = ConfigDict(use_enum_values=True)
    type: EscalatorType = EscalatorType.NONE
    pct: float | None = None
    cap_pct: float | None = None


class TierBand(BaseModel):
    """A volume band, e.g. units 1-250 @ $21.50. `marginal` = applies only within the band."""

    min_units: int = Field(ge=0)
    max_units: int | None = None  # None = open-ended (e.g. "501+")
    amount: float | None = None
    rate_pct: float | None = None
    marginal: bool = True


class Condition(BaseModel):
    model_config = ConfigDict(use_enum_values=True)
    type: ConditionType
    description: str
    trigger: str | None = None  # machine-checkable flag, e.g. "telematics_enrolled"
    threshold_amount: float | None = None
    share_pct: float | None = None  # for rebate_share / retention splits

    @field_validator("type", mode="before")
    @classmethod
    def _coerce_type(cls, v):
        return _coerce_enum(v, ConditionType, ConditionType.OTHER)


class FeeItem(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    description: str
    fee_type: FeeType
    amount: float | None = None  # absolute $ amount, when applicable
    currency: str = "USD"
    rate_pct: float | None = None  # for percentage / cost_plus markups
    unit_basis: UnitBasis | None = None
    minimum: float | None = None
    tier_bands: list[TierBand] = Field(default_factory=list)
    conditions: list[Condition] = Field(default_factory=list)
    escalator: Escalator | None = None

    @field_validator("fee_type", mode="before")
    @classmethod
    def _coerce_fee_type(cls, v):
        return _coerce_enum(v, FeeType, FeeType.OTHER)

    @field_validator("unit_basis", mode="before")
    @classmethod
    def _coerce_unit_basis(cls, v):
        return _coerce_enum(v, UnitBasis, UnitBasis.OTHER)


class ServiceLineTerms(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    service: ServiceLine
    elected: bool
    fee_items: list[FeeItem] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    citations: list[Citation] = Field(default_factory=list)
    notes: str | None = None


class BundledFee(BaseModel):
    """A single fee that covers several service lines at once (e.g. a volume-tiered
    'Bundled Management Fee' covering maintenance + insurance admin + mileage). Modeled at
    the top level because it does not belong to one service line; the covered services are
    still marked elected=true and note that they are billed via this bundle."""

    model_config = ConfigDict(use_enum_values=True)

    description: str
    covers_services: list[ServiceLine] = Field(default_factory=list)
    fee_items: list[FeeItem] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    citations: list[Citation] = Field(default_factory=list)


class DepreciationRate(BaseModel):
    vehicle_class: str
    monthly_pct: float


class LeaseRate(BaseModel):
    model_config = ConfigDict(use_enum_values=True)
    basis: RateBasis
    index: str | None = None  # e.g. "SOFR_30d_avg"
    spread_bps: int | None = None
    fixed_rate_pct: float | None = None


class EarlyTermination(BaseModel):
    formula: str
    components: list[str] = Field(default_factory=list)


class LeaseTerms(BaseModel):
    lease_type: str = "open_end_TRAC"
    rate: LeaseRate
    depreciation: list[DepreciationRate] = Field(default_factory=list)
    admin_fee: FeeItem | None = None
    early_termination: EarlyTermination | None = None
    trac_surplus_split: str | None = None
    billing_frequency: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    citations: list[Citation] = Field(default_factory=list)


class ContractExtraction(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    doc_type: DocType
    client_name: str
    effective_date: str | None = None
    escalator: Escalator | None = None
    payment_terms: str | None = None
    true_up: str | None = None
    service_lines: list[ServiceLineTerms] = Field(default_factory=list)
    bundled_fees: list[BundledFee] = Field(default_factory=list)  # cross-service fees
    lease_terms: LeaseTerms | None = None  # MLA only
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)

    def line(self, service: ServiceLine) -> ServiceLineTerms | None:
        for sl in self.service_lines:
            if sl.service == service.value or sl.service == service:
                return sl
        return None
