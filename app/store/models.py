"""Stored domain models (distinct from the extraction schema).

These are the shapes persisted to DynamoDB / returned by the API. Money values inside
`fee_items` / `citations` are stored as-is (converted to Decimal at the DynamoDB boundary by
the repository).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from ..lifecycle.submission_state import Role, SubmissionStatus


class EngagementScope(str, Enum):
    """What the customer bought — drives which master agreements are required."""

    LEASE_ONLY = "LEASE_ONLY"
    SERVICE_ONLY = "SERVICE_ONLY"
    LEASE_AND_SERVICE = "LEASE_AND_SERVICE"


# Lease -> Master Lease Agreement, service -> Master Service Agreement.
REQUIRED_DOC_TYPES: dict[str, tuple[str, ...]] = {
    EngagementScope.LEASE_ONLY.value: ("MLA",),
    EngagementScope.SERVICE_ONLY.value: ("MSA",),
    EngagementScope.LEASE_AND_SERVICE.value: ("MLA", "MSA"),
}


_DEFAULT_DOC_TYPES = REQUIRED_DOC_TYPES[EngagementScope.LEASE_AND_SERVICE.value]


def required_doc_types(scope: str | None) -> tuple[str, ...]:
    """Unknown or missing scope falls back to both, so un-backfilled rows behave as before."""
    return REQUIRED_DOC_TYPES.get(scope or "", _DEFAULT_DOC_TYPES)


class Engagement(BaseModel):
    engagement_id: str
    name: str
    client_name: str  # denormalized display copy of Customer.legal_name (lists need no join)
    customer_id: str | None = None  # null only for rows predating the customer backfill
    # Derived from the agreements in force, not chosen at creation. None until the first
    # document has been classified.
    scope: str | None = None
    status: str = "DRAFT"  # denormalized submission lifecycle status (for lists + finance)
    # When it last moved. Denormalized beside the status for the same reason the status is:
    # lists need "how long has this been sitting here" without reading every submission, and
    # created_at cannot answer it — that is the age of the deal, not of the wait.
    status_since: str | None = None
    fleet_size: int = 100  # effective vehicles under management; drives recurring dues
    # When set, overrides the count of vehicles assigned to this engagement.
    fleet_size_override: int | None = None
    monthly_recurring: float | None = None  # computed at billing setup / fleet change
    billing_start: str | None = None  # ISO date billing became active (schedule anchor)
    billing_frequency: str = "monthly"  # monthly | quarterly | annual
    # --- contract term ---
    # A master agreement runs for a fixed term and then renews or lapses. `contract_end` is the
    # date the current term expires; renewal notice is the lead time the agreement requires.
    contract_start: str | None = None  # ISO date the agreement took effect
    contract_term_months: int | None = None
    contract_end: str | None = None  # ISO date the current term expires
    auto_renew: bool = False
    renewal_notice_days: int = 90
    created_by: str
    created_at: str


class Customer(BaseModel):
    """A leasing client. One customer holds many engagements (repeat deals over time)."""

    customer_id: str
    legal_name: str
    display_name: str | None = None
    industry: str | None = None
    billing_address: str | None = None
    city: str | None = None
    country: str | None = None
    primary_contact_email: str | None = None
    primary_contact_phone: str | None = None
    status: str = "ACTIVE"  # ACTIVE | INACTIVE
    notes: str | None = None
    created_by: str
    created_at: str
    updated_at: str | None = None


class VehicleOwnership(str, Enum):
    """CUSTOMER_OWNED is the third-party case: Wheels sells services, not the asset."""

    WHEELS_OWNED = "WHEELS_OWNED"
    CUSTOMER_OWNED = "CUSTOMER_OWNED"


class DutyBand(str, Enum):
    LIGHT_DUTY = "LIGHT_DUTY"
    MEDIUM_DUTY = "MEDIUM_DUTY"
    HEAVY_DUTY = "HEAVY_DUTY"
    EQUIPMENT = "EQUIPMENT"


class BodyClass(str, Enum):
    SEDAN = "SEDAN"
    SUV = "SUV"
    MINIVAN = "MINIVAN"
    CARGO_VAN = "CARGO_VAN"
    PICKUP = "PICKUP"
    BOX_TRUCK = "BOX_TRUCK"
    STAKE_FLATBED = "STAKE_FLATBED"
    SERVICE_BODY = "SERVICE_BODY"
    STEP_VAN = "STEP_VAN"
    TRACTOR = "TRACTOR"
    VOCATIONAL_TRUCK = "VOCATIONAL_TRUCK"
    TRAILER = "TRAILER"
    SPECIALTY_UPFIT = "SPECIALTY_UPFIT"
    FORKLIFT = "FORKLIFT"


class Powertrain(str, Enum):
    ICE = "ICE"
    HYBRID = "HYBRID"
    PHEV = "PHEV"
    BEV = "BEV"


class VehicleStatus(str, Enum):
    ON_ORDER = "ON_ORDER"
    IN_STOCK = "IN_STOCK"  # available to lease (WHEELS_OWNED only)
    ASSIGNED = "ASSIGNED"
    IN_MAINTENANCE = "IN_MAINTENANCE"
    RETIRED = "RETIRED"


class LeaseStructure(str, Enum):
    """Wheels-owned units only; matches the lease products Wheels offers."""

    TRAC_OPEN_END = "TRAC_OPEN_END"
    CLOSED_END = "CLOSED_END"
    FMV = "FMV"
    CAPITAL = "CAPITAL"


# Default body class per duty band, used to keep the two axes coherent on write.
DUTY_BY_BODY: dict[str, str] = {
    BodyClass.SEDAN.value: DutyBand.LIGHT_DUTY.value,
    BodyClass.SUV.value: DutyBand.LIGHT_DUTY.value,
    BodyClass.MINIVAN.value: DutyBand.LIGHT_DUTY.value,
    BodyClass.CARGO_VAN.value: DutyBand.LIGHT_DUTY.value,
    BodyClass.PICKUP.value: DutyBand.LIGHT_DUTY.value,
    BodyClass.BOX_TRUCK.value: DutyBand.MEDIUM_DUTY.value,
    BodyClass.STAKE_FLATBED.value: DutyBand.MEDIUM_DUTY.value,
    BodyClass.SERVICE_BODY.value: DutyBand.MEDIUM_DUTY.value,
    BodyClass.STEP_VAN.value: DutyBand.MEDIUM_DUTY.value,
    BodyClass.TRACTOR.value: DutyBand.HEAVY_DUTY.value,
    BodyClass.VOCATIONAL_TRUCK.value: DutyBand.HEAVY_DUTY.value,
    BodyClass.TRAILER.value: DutyBand.HEAVY_DUTY.value,
    BodyClass.SPECIALTY_UPFIT.value: DutyBand.MEDIUM_DUTY.value,
    BodyClass.FORKLIFT.value: DutyBand.EQUIPMENT.value,
}


class Vehicle(BaseModel):
    """One physical unit in the inventory (stored in the separate vehicles table)."""

    vehicle_id: str
    vin: str | None = None
    unit_number: str | None = None  # the customer's own asset tag
    year: int | None = None
    make: str | None = None
    model: str | None = None
    duty_band: str = DutyBand.LIGHT_DUTY.value
    body_class: str = BodyClass.SEDAN.value
    powertrain: str = Powertrain.ICE.value
    ownership: str = VehicleOwnership.WHEELS_OWNED.value
    status: str = VehicleStatus.IN_STOCK.value
    # Set for CUSTOMER_OWNED units, and denormalized from the engagement once assigned.
    customer_id: str | None = None
    engagement_id: str | None = None
    assigned_at: str | None = None
    lease_structure: str | None = None  # WHEELS_OWNED only
    lease_term_months: int | None = None  # 24..120
    in_service_date: str | None = None
    odometer: int | None = None
    notes: str | None = None
    created_by: str
    created_at: str
    updated_at: str | None = None

    @model_validator(mode="after")
    def _invariants(self):
        # Keep the two type axes coherent: body class determines the duty band.
        self.duty_band = DUTY_BY_BODY.get(self.body_class, self.duty_band)
        if self.ownership == VehicleOwnership.CUSTOMER_OWNED.value:
            # A third-party unit is never ours to lease out, so it can never be in stock.
            # Enforcing it here means availability is a pure status query with no ownership
            # filter at read time.
            if self.status == VehicleStatus.IN_STOCK.value:
                raise ValueError("a customer-owned vehicle cannot be IN_STOCK")
            if not self.customer_id:
                raise ValueError("a customer-owned vehicle requires a customer_id")
            self.lease_structure = None
            self.lease_term_months = None
        return self

    @property
    def is_available(self) -> bool:
        return (
            self.ownership == VehicleOwnership.WHEELS_OWNED.value
            and self.status == VehicleStatus.IN_STOCK.value
        )


class VehicleAssignment(BaseModel):
    """Append-only history of a vehicle moving on and off engagements."""

    vehicle_id: str
    assignment_id: str
    engagement_id: str
    customer_id: str | None = None
    assigned_at: str
    released_at: str | None = None
    assigned_by: str | None = None


class ProviderUser(BaseModel):
    """Org-level (Wheels-side) provider directory entry — not scoped to any engagement."""

    user_id: str
    email: str
    name: str | None = None
    phone: str | None = None
    onboarded: bool = False
    created_at: str


class Membership(BaseModel):
    engagement_id: str
    user_id: str
    email: str
    role: Role
    name: str | None = None
    phone: str | None = None
    created_at: str


class UserProfile(BaseModel):
    user_id: str
    email: str
    name: str | None = None
    phone: str | None = None
    onboarded: bool = False
    updated_at: str | None = None


class Submission(BaseModel):
    """One review cycle over an engagement's agreements.

    An engagement has one cycle for its original agreements and a further cycle for each
    amendment afterwards — a renewal, an added lease or service, or a corrected version of a
    document. Cycles are numbered from 1 and only one may be open at a time; the most recent
    cycle to reach ACTIVE holds the terms actually in force and billing.
    """

    engagement_id: str
    submission_id: str
    status: SubmissionStatus = SubmissionStatus.DRAFT
    cycle: int = 1
    # Document slots keyed by doc_type. The two legacy scalar fields are still written so
    # rows created before this change (and any reader that has not been updated) keep working;
    # `docs()` is the accessor everything should use.
    document_ids: dict[str, str] = Field(default_factory=dict)
    msa_document_id: str | None = None
    mla_document_id: str | None = None
    latest_comment: str | None = None
    latest_comment_by: str | None = None  # display name of who left latest_comment
    client_signature: dict[str, Any] | None = None  # {full_name, place, signed_at}
    change_review: dict[str, Any] | None = None  # cached re-upload vs requested-change assessment
    nego_summary: dict[str, Any] | None = None  # cached negotiation summary (thread-count keyed)
    created_at: str
    updated_at: str

    def docs(self) -> dict[str, str]:
        """Slot map, hydrated from the legacy scalar fields when the dict is empty."""
        if self.document_ids:
            return dict(self.document_ids)
        legacy = {"MSA": self.msa_document_id, "MLA": self.mla_document_id}
        return {t: d for t, d in legacy.items() if d}

    def with_doc(self, doc_type: str, document_id: str) -> dict[str, Any]:
        """The attribute updates that set one slot, keeping the legacy fields in sync."""
        slots = self.docs()
        slots[doc_type] = document_id
        patch: dict[str, Any] = {"document_ids": slots}
        if doc_type == "MSA":
            patch["msa_document_id"] = document_id
        elif doc_type == "MLA":
            patch["mla_document_id"] = document_id
        return patch


class DocumentStanding(str, Enum):
    """Whether an agreement governs the engagement today, or is kept for the record."""

    CURRENT = "CURRENT"
    SUPERSEDED = "SUPERSEDED"


class Document(BaseModel):
    engagement_id: str
    document_id: str
    doc_type: str = "UNKNOWN"  # MSA | MLA | UNKNOWN until classified from the text
    filename: str
    current_version: int = 1
    # --- classification, filled in by the parse worker ---
    standing: str = DocumentStanding.CURRENT.value
    effective_date: str | None = None  # read from the document; orders one against another
    classified_type: str | None = None  # what the text said, before any manual override
    classification_confidence: float | None = None
    type_overridden: bool = False  # a person corrected the type; do not re-classify
    # Which review cycle this agreement arrived in, so an amendment can show what it added
    # without re-reading the documents that were already in force.
    cycle: int = 1
    created_at: str


class DocumentVersion(BaseModel):
    engagement_id: str
    document_id: str
    version: int
    s3_key: str
    page_count: int | None = None
    status: str = "uploaded"  # uploaded | parsed | extracted | failed
    uploaded_at: str
    # What the extraction run cost and whether every call came back whole. Persisted because
    # tokens, model and price are otherwise invisible, and a prompt cache that quietly stopped
    # working looks exactly like one that is fine.
    extraction_run: dict[str, Any] | None = None
    # Full ContractExtraction JSON (small); the review queue is materialized as ReviewFields.
    extraction: dict[str, Any] | None = None


class TermRow(BaseModel):
    """One extracted contract term, of any of the nine record types.

    Replaces the per-service-line review field. The headline fields are denormalised off the
    record body so a list of three hundred terms renders without parsing each one, while
    `record` keeps the full typed payload that the analyst actually edits.
    """

    engagement_id: str
    document_id: str
    version: int
    record_id: str  # deterministic: re-extraction replaces rather than duplicates
    category: str  # pricing | sla | reporting | misc
    info_type: str

    # --- denormalised for lists ---
    title: str
    subtitle: str | None = None
    amount: float | None = None
    frequency: str | None = None  # the contract's own wording
    unit_basis: str | None = None  # normalised, so the estimate and the interface agree
    billing_class: str | None = None  # recurring | recurring_per_driver | usage | one_time | credit

    # --- catalog resolution (pricing rows) ---
    program_id: str | None = None
    catalog_item_id: str | None = None
    catalog_match: str | None = None  # exact | alias | normalised | fuzzy | unmatched

    # --- review state ---
    confidence: float = 0.0
    needs_review: bool = False
    approved: bool = False
    corrected: bool = False
    # Set when a re-extraction changed a term the analyst had already approved, so the change
    # is visible rather than silently un-approving a row they thought was settled.
    changed_since_approval: bool = False
    # A term the latest extraction no longer finds. Kept rather than deleted: an agreement's
    # history is the point of the audit trail.
    superseded: bool = False

    record: dict[str, Any] = Field(default_factory=dict)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    notes: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class EngagementService(BaseModel):
    """A program this engagement's agreements show it avails.

    Written per document version so that removing an agreement withdraws exactly the coverage
    it contributed, and queried two ways off keys that already exist: by engagement for the
    coverage tab, by program through GSI2 for the org-wide services page.
    """

    engagement_id: str
    program_id: str
    program_name: str
    document_id: str
    version: int
    item_ids: list[str] = Field(default_factory=list)
    priced_item_count: int = 0
    created_at: str | None = None


class ReviewField(BaseModel):
    engagement_id: str
    document_id: str
    version: int
    field_id: str
    service: str
    elected: bool
    confidence: float
    needs_review: bool
    approved: bool = False
    corrected: bool = False
    fee_items: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    notes: str | None = None


class Payment(BaseModel):
    """One scheduled payment in an engagement's billing schedule.

    Amounts for unpaid rows are computed at read time from the engagement's current
    `monthly_recurring` (so a fleet change reflows into future dues); paid rows freeze
    `amount_paid`. Due dates are fixed at generation time.
    """

    engagement_id: str
    submission_id: str
    seq: int
    kind: str  # "initial" | "recurring"
    label: str
    period_start: str  # ISO date
    due_date: str  # ISO date
    paid: bool = False
    paid_at: str | None = None
    paid_by: str | None = None
    amount_paid: float | None = None


class AuditEvent(BaseModel):
    engagement_id: str
    event_id: str
    ts: str
    actor_id: str
    actor_role: str
    actor_name: str | None = None
    action: str
    target: str | None = None
    comment: str | None = None
