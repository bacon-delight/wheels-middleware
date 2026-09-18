"""Single-table key construction (see plan's DynamoDB design).

Confidence is encoded so the GSI1 review query surfaces the risky fields first: needs-review
before ok, then ascending confidence.
"""

from __future__ import annotations


def eng_pk(engagement_id: str) -> str:
    return f"ENG#{engagement_id}"


def user_pk(user_id: str) -> str:
    return f"USER#{user_id}"


def profile_sk() -> str:
    return "#PROFILE"


def org_providers_pk() -> str:
    return "ORG#PROVIDERS"


def engagement_meta_sk() -> str:
    return "#META"


def membership_sk(user_id: str) -> str:
    return f"USER#{user_id}"


def submission_sk(submission_id: str) -> str:
    return f"SUB#{submission_id}"


def document_sk(document_id: str) -> str:
    return f"DOC#{document_id}"


def version_sk(document_id: str, version: int) -> str:
    return f"DOC#{document_id}#V#{version:04d}"


def field_sk(document_id: str, version: int, service: str, field_id: str) -> str:
    return f"DOC#{document_id}#V#{version:04d}#FLD#{service}#{field_id}"


def audit_sk(ts: str, event_id: str) -> str:
    return f"AUD#{ts}#{event_id}"


def approved_sk(submission_id: str) -> str:
    return f"SUB#{submission_id}#APPROVED"


def payment_sk(seq: int) -> str:
    return f"PAY#{seq:04d}"


# --- GSI1 (overloaded) ---
def user_gsi1pk(user_id: str) -> str:
    return f"USER#{user_id}"


def doc_version_gsi1pk(document_id: str, version: int) -> str:
    return f"DOC#{document_id}#V#{version:04d}"


def review_gsi1sk(needs_review: bool, confidence: float) -> str:
    # needs-review (0) sorts before ok (1); then ascending confidence (000..100).
    needs = "0" if needs_review else "1"
    conf = f"{int(round(confidence * 100)):03d}"
    return f"RV#{needs}#C#{conf}"


def lcstatus_gsi1pk(status: str) -> str:
    return f"LCSTATUS#{status}"


# --- Customers (main table) ---
def org_customers_pk() -> str:
    """All customers live in one small partition, mirroring ORG#PROVIDERS."""
    return "ORG#CUSTOMERS"


def customer_sk(customer_id: str) -> str:
    return f"CUST#{customer_id}"


# --- GSI2 (customer -> engagements) ---
def customer_gsi2pk(customer_id: str) -> str:
    return f"CUST#{customer_id}"


# --- Vehicles (separate table) ---
def vehicle_pk(vehicle_id: str) -> str:
    return f"VEH#{vehicle_id}"


def vehicle_meta_sk() -> str:
    return "#META"


def assignment_sk(ts: str, assignment_id: str) -> str:
    return f"ASG#{ts}#{assignment_id}"


def fleet_gsi1pk(ownership: str) -> str:
    """Inventory partition, one per ownership class (WHEELS_OWNED / CUSTOMER_OWNED)."""
    return f"FLEET#{ownership}"


def vehicle_gsi1sk(status: str, duty_band: str, vehicle_id: str) -> str:
    """Sorts by status then duty band so the inventory page can begins_with-slice either."""
    return f"ST#{status}#DUTY#{duty_band}#VEH#{vehicle_id}"


def vehicle_gsi2pk(engagement_id: str) -> str:
    """GSI2 is sparse: only assigned vehicles carry it, so there is no hot 'unassigned'
    partition. Unassigned stock is listed off GSI1 with a status prefix instead."""
    return f"ENG#{engagement_id}"


def vehicle_gsi2sk(vehicle_id: str) -> str:
    return f"VEH#{vehicle_id}"
