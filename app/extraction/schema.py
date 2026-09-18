"""The extraction contract.

These Pydantic models are the single source of truth for (a) the LLM tool-use schema,
(b) validation of model output, and (c) the shape stored in DynamoDB / shown in the review UI.

A contract says far more than what it charges. The shape here follows how a fleet analyst
actually reads one: **pricing**, **service levels**, **reporting obligations**, and everything
else — definitions, online tools, who is responsible for what, signatures. Nine record types in
four categories, which between them held all 158 records of a hand-made extraction of the
Walmart statement of work (`tests/fixtures/walmart_ground_truth.json`).

Two decisions here are load-bearing:

**Nothing about a service is an enum.** `program` and `item` are free text copied from the
contract, resolved against a database-backed catalog *after* validation. The previous schema
constrained the service to a thirteen-member enum with no coercion, so a single unrecognised
service name failed `model_validate` and lost the whole document. A catalog that the business
grows cannot live in a Python enum.

**Records are validated one at a time.** A malformed record is flagged and skipped; it does not
take the other 157 with it.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Annotated, Any, Literal

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


def _as_text(value):
    """Accept a number where prose is expected.

    Both people and models write a bare `0.25` into a column meant for words — the source
    workbook does exactly this for a fee-credit multiplier. Rejecting it would lose the record
    over a formatting difference, so it is stringified and kept verbatim.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int | float):
        return repr(value) if isinstance(value, float) else str(value)
    return value


SCHEMA_VERSION = 2


class Category(str, Enum):
    """The four tabs an analyst reads a contract through."""

    PRICING = "pricing"
    SLA = "sla"
    REPORTING = "reporting"
    MISC = "misc"


class InfoType(str, Enum):
    PRICING_ITEM = "pricing_item"
    SLA_ITEM = "sla_item"
    REPORTING_REQUIREMENT = "reporting_requirement"
    DEFINITION = "definition"
    ONLINE_TOOL = "online_tool"
    RESPONSIBILITY = "responsibility"
    SIGNATURE = "signature"
    INFORMATION_SECTION = "information_section"
    # A section worth keeping that fits none of the above. The human extraction called this
    # "TBD": payment terms, insurance requirements, auditability, penalties. Recording it as
    # uncategorised keeps it visible instead of dropping it on the floor.
    UNCATEGORISED = "uncategorised"


CATEGORY_OF: dict[str, str] = {
    InfoType.PRICING_ITEM.value: Category.PRICING.value,
    InfoType.SLA_ITEM.value: Category.SLA.value,
    InfoType.REPORTING_REQUIREMENT.value: Category.REPORTING.value,
    InfoType.DEFINITION.value: Category.MISC.value,
    InfoType.ONLINE_TOOL.value: Category.MISC.value,
    InfoType.RESPONSIBILITY.value: Category.MISC.value,
    InfoType.SIGNATURE.value: Category.MISC.value,
    InfoType.INFORMATION_SECTION.value: Category.MISC.value,
    InfoType.UNCATEGORISED.value: Category.MISC.value,
}

# Human-readable labels, matching the column headings of the source workbook.
INFO_TYPE_LABELS: dict[str, str] = {
    InfoType.PRICING_ITEM.value: "Pricing Item",
    InfoType.SLA_ITEM.value: "SLA Item",
    InfoType.REPORTING_REQUIREMENT.value: "Reporting Requirement",
    InfoType.DEFINITION.value: "Definition",
    InfoType.ONLINE_TOOL.value: "Online Tool",
    InfoType.RESPONSIBILITY.value: "Responsibility",
    InfoType.SIGNATURE.value: "Signature",
    InfoType.INFORMATION_SECTION.value: "Information Section",
    InfoType.UNCATEGORISED.value: "Uncategorised",
}


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
    # New: contracts price MVR monitoring and safety training per driver, which a per-vehicle
    # estimate cannot consume. Naming it keeps those fees visible instead of silently dropped.
    PER_DRIVER_PER_MONTH = "per_driver_per_month"
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


class Party(str, Enum):
    """Who owes a duty. `VENDOR` is Wheels; `CLIENT` is the customer."""

    VENDOR = "vendor"
    CLIENT = "client"
    BOTH = "both"
    OTHER = "other"


class BBoxModel(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class Citation(BaseModel):
    """Where a value came from. `bbox` is resolved server-side from `quote`, not the LLM."""

    model_config = ConfigDict(use_enum_values=True)

    # Optional because the resolver is authoritative: it locates the quote in the page geometry
    # and overwrites whatever page the model reported. A citation with a good quote and no page
    # still resolves, so requiring one here would only throw away usable evidence.
    page: int | None = Field(default=None, ge=1)
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

    @field_validator("type", mode="before")
    @classmethod
    def _coerce_type(cls, v):
        return _coerce_enum(v, EscalatorType, EscalatorType.NONE)


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


# --------------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------------


class TermBase(BaseModel):
    """What every extracted record carries, whatever its type."""

    model_config = ConfigDict(use_enum_values=True)

    contract_section: str | None = None  # "Pricing Schedule", "Service Level Agreement"
    citations: list[Citation] = Field(default_factory=list)
    notes: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class PricingItem(TermBase):
    """One priced line, or a program named with nothing priced under it.

    A program with no `item` is not an empty record: it is the contract saying the customer is
    enrolled in that program without a separate charge, which is exactly what makes coverage
    answerable. Twelve of the Walmart workbook's 34 pricing rows are this shape.
    """

    info_type: Literal["pricing_item"] = "pricing_item"

    program: str  # verbatim from the contract: "Fuel Management Program"
    product_service: str | None = None  # "Fuel Card"
    item: str | None = None  # None => program named but unpriced
    sub_category: str | None = None  # "PASSENGER/LIGHT-DUTY VEHICLES (under 10,001 GVWR)"
    frequency: str | None = None  # verbatim: "pvpm", "per card", "per driver per month"
    amount: float | None = None
    currency: str = "USD"
    calculation: str | None = None  # "Ten percent (10%) of ISP Charges per transaction"
    included: bool = False  # the contract prices this at no charge ("Included")

    # --- structured billing annexe, derived server-side where the text allows ---
    fee_type: FeeType = FeeType.OTHER
    unit_basis: UnitBasis | None = None
    rate_pct: float | None = None
    rate_index: str | None = None  # lease fold-in: "SOFR_30d_avg"
    spread_bps: int | None = None  # lease fold-in: 235
    minimum: float | None = None
    maximum: float | None = None  # "minimum of $5.00 and maximum of $35.00 per transaction"
    tier_bands: list[TierBand] = Field(default_factory=list)
    conditions: list[Condition] = Field(default_factory=list)
    escalator: Escalator | None = None

    # --- catalog resolution: assigned by the server, never by the model ---
    program_id: str | None = None
    item_id: str | None = None
    catalog_match: str | None = None  # exact | alias | normalised | fuzzy | unmatched

    @field_validator("frequency", "calculation", "sub_category", "item", mode="before")
    @classmethod
    def _text(cls, v):
        return _as_text(v)

    @field_validator("fee_type", mode="before")
    @classmethod
    def _coerce_fee_type(cls, v):
        return _coerce_enum(v, FeeType, FeeType.OTHER)

    @field_validator("unit_basis", mode="before")
    @classmethod
    def _coerce_unit_basis(cls, v):
        return _coerce_enum(v, UnitBasis, UnitBasis.OTHER)


class SlaItem(TermBase):
    info_type: Literal["sla_item"] = "sla_item"

    contract_sub_section: str | None = None
    category: str  # "Driver Contact Center (Answer)"
    # Optional, because a service level agreement holds the remedies as well as the targets.
    # A fee-credit row has a category and a formula but no standard to meet.
    service_level_standard: str | None = None
    frequency: str | None = None  # "Monthly"
    # A string, not a number: the contract says "N/A" as often as it says "50 requests".
    minimum_threshold: str | None = None
    calculation: str | None = None
    example: str | None = None
    program: str | None = None
    program_id: str | None = None

    @field_validator("minimum_threshold", "calculation", "example", mode="before")
    @classmethod
    def _text(cls, v):
        return _as_text(v)


class ReportingRequirement(TermBase):
    info_type: Literal["reporting_requirement"] = "reporting_requirement"

    report_name: str
    report_specifications: str | None = None
    frequency: str | None = None
    # A list because one report can serve several programs, e.g. the Downtime Report covers
    # both Maintenance Assistance and Collision Management.
    applicable_programs: list[str] = Field(default_factory=list)
    program_ids: list[str] = Field(default_factory=list)


class Definition(TermBase):
    info_type: Literal["definition"] = "definition"

    term: str
    definition: str


class OnlineTool(TermBase):
    info_type: Literal["online_tool"] = "online_tool"

    platform: str | None = None  # "The FleetView Platform"
    tool_name: str
    description: str | None = None


class Responsibility(TermBase):
    info_type: Literal["responsibility"] = "responsibility"

    topic: str | None = None
    task: str
    responsible_party: str | None = None  # verbatim, e.g. "Vendor", "Walmart"
    party: Party = Party.OTHER  # normalised
    timing: str | None = None
    frequency: str | None = None

    @field_validator("timing", "frequency", mode="before")
    @classmethod
    def _text(cls, v):
        return _as_text(v)

    @field_validator("party", mode="before")
    @classmethod
    def _coerce_party(cls, v):
        return _coerce_enum(v, Party, Party.OTHER)


class Signature(TermBase):
    info_type: Literal["signature"] = "signature"

    company: str
    signed_date_raw: str | None = None  # "April 4, 2025 | 17:05 CDT"
    signed_at: str | None = None  # ISO, parsed server-side
    name: str | None = None
    title: str | None = None


class InformationSection(TermBase):
    info_type: Literal["information_section"] = "information_section"

    topic: str
    description: str


class Uncategorised(TermBase):
    info_type: Literal["uncategorised"] = "uncategorised"

    item: str
    detail: str | None = None


TermRecord = Annotated[
    PricingItem
    | SlaItem
    | ReportingRequirement
    | Definition
    | OnlineTool
    | Responsibility
    | Signature
    | InformationSection
    | Uncategorised,
    Field(discriminator="info_type"),
]

RECORD_MODELS: dict[str, type[TermBase]] = {
    InfoType.PRICING_ITEM.value: PricingItem,
    InfoType.SLA_ITEM.value: SlaItem,
    InfoType.REPORTING_REQUIREMENT.value: ReportingRequirement,
    InfoType.DEFINITION.value: Definition,
    InfoType.ONLINE_TOOL.value: OnlineTool,
    InfoType.RESPONSIBILITY.value: Responsibility,
    InfoType.SIGNATURE.value: Signature,
    InfoType.INFORMATION_SECTION.value: InformationSection,
    InfoType.UNCATEGORISED.value: Uncategorised,
}

# Which record types each extraction call is allowed to return. Splitting misc in two keeps
# definitions, which are long and numerous, from crowding out the rest of the output budget.
CALL_RECORD_TYPES: dict[str, tuple[str, ...]] = {
    "pricing": (InfoType.PRICING_ITEM.value,),
    "sla": (InfoType.SLA_ITEM.value,),
    "reporting": (InfoType.REPORTING_REQUIREMENT.value,),
    # Definitions get a call to themselves. Sharing one with tools and signatures produced
    # none at all: a contract carries dozens of defined terms, and asked for four kinds of
    # record at once the model answered the other three and stopped.
    "definitions": (InfoType.DEFINITION.value,),
    "misc_reference": (
        InfoType.ONLINE_TOOL.value,
        InfoType.INFORMATION_SECTION.value,
        InfoType.SIGNATURE.value,
    ),
    "misc_operational": (
        InfoType.RESPONSIBILITY.value,
        InfoType.UNCATEGORISED.value,
    ),
}


# --------------------------------------------------------------------------------------------
# Document envelope
# --------------------------------------------------------------------------------------------


class DocumentMeta(BaseModel):
    """Facts about the document as a whole, rather than about any one term."""

    model_config = ConfigDict(use_enum_values=True)

    # A plain string, not an enum: the corpus holds statements of work, professional services
    # agreements and amendments as well as master agreements, and an unrecognised kind of
    # document should still extract.
    doc_type: str = "UNKNOWN"
    client_name: str | None = None
    # The document's own identifier, e.g. a DocuSign envelope id from the page footer. Read by
    # the server from page 1, because a contract with an amendment carries more than one.
    document_name: str | None = None
    effective_date: str | None = None
    payment_terms: str | None = None
    true_up: str | None = None
    escalator: Escalator | None = None
    billing_frequency: str | None = None  # drives the payment schedule
    lease_type: str | None = None  # "open_end_TRAC"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ContractExtraction(BaseModel):
    """Everything read out of one document version."""

    model_config = ConfigDict(use_enum_values=True)

    schema_version: int = SCHEMA_VERSION
    doc_meta: DocumentMeta = Field(default_factory=DocumentMeta)
    records: list[TermRecord] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    def by_category(self, category: str) -> list[Any]:
        return [r for r in self.records if CATEGORY_OF.get(r.info_type) == category]

    def by_type(self, info_type: str) -> list[Any]:
        return [r for r in self.records if r.info_type == info_type]

    def counts_by_category(self) -> dict[str, int]:
        out = {c.value: 0 for c in Category}
        for r in self.records:
            key = CATEGORY_OF.get(r.info_type)
            if key:
                out[key] += 1
        return out


def coerce_record_list(raw: Any) -> list[Any]:
    """Get a list of records out of whatever the model actually sent.

    Faced with a large nested schema, a model will sometimes serialise an array as a JSON
    string rather than as an array. That is not an error we can afford to be strict about: it
    looked here exactly like a document containing no terms at all, which is the most
    expensive way for extraction to fail — silently and plausibly.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        for candidate in (text, _repair_inner_quotes(text)):
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            return decoded if isinstance(decoded, list) else [decoded]
        return _salvage_json_objects(_repair_inner_quotes(text))
    if isinstance(raw, dict):
        return [raw]
    return []


def _repair_inner_quotes(text: str) -> str:
    """Escape quotation marks a model left unescaped inside a JSON string value.

    Contracts are full of quoted definitions — `"Statement of Work" means ...` — and a model
    serialising them into a JSON string routinely copies those quotes through without escaping
    them, which makes the whole array unparseable. A closing quote is only real when the next
    non-space character is one that can legally follow a string; anything else is content.

    This is a repair, not a parser. It runs only after `json.loads` has already failed.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            continue
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            nxt = next((c for c in text[i + 1 :] if not c.isspace()), "")
            if nxt in {",", ":", "}", "]", ""}:
                out.append(ch)
                in_string = False
            else:
                out.append('\\"')  # content, not a delimiter
            continue
        out.append(ch)
    return "".join(out)


def _salvage_json_objects(text: str) -> list[Any]:
    """Recover the complete objects from a truncated JSON array.

    A response cut off at its token limit leaves a valid prefix and a half-written final
    object. Thirty good records are worth far more than a clean failure, so the balanced
    objects are scanned out and the incomplete tail dropped.
    """
    out: list[Any] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    out.append(json.loads(text[start : i + 1]))
                except json.JSONDecodeError:
                    pass
                start = -1
    return out


def parse_records(raw: Any) -> tuple[list[Any], list[str]]:
    """Validate records one at a time.

    Returns the records that validated and a reason for each that did not. Per-record parsing is
    the whole point: the previous schema validated the document as a unit, so one bad value in
    one record lost every other record in the contract.
    """
    good: list[Any] = []
    bad: list[str] = []
    for i, item in enumerate(coerce_record_list(raw)):
        if not isinstance(item, dict):
            bad.append(f"record {i}: expected an object, got {type(item).__name__}")
            continue
        info_type = item.get("info_type")
        model = RECORD_MODELS.get(info_type)
        if model is None:
            bad.append(f"record {i}: unknown info_type {info_type!r}")
            continue
        try:
            good.append(model.model_validate(item))
        except Exception as e:  # noqa: BLE001 - one bad record must not lose the rest
            bad.append(f"record {i} ({info_type}): {type(e).__name__}: {e}")
    return good, bad
