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
