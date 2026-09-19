"""The Submission approval state machine.

One review cycle (a Submission) bundles an engagement's MSA + MLA and moves through this
machine. Transitions are triggered by human API actions or one machine step (sanity
validation). This module is pure logic — no I/O — so the whole workflow is defined and
tested in one place (the "one place to see the workflow" benefit without a workflow engine).

Finance is folded into the provider role for now: finance actions permit the FINANCE role,
and PROVIDER is treated as also holding finance permission (splitting later is a one-line
change to the allowed-roles set).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Role(str, Enum):
    PROVIDER = "provider"  # Wheels-side analyst (also holds finance permission for now)
    CLIENT = "client"
    FINANCE = "finance"
    SYSTEM = "system"  # pipeline / machine steps


class SubmissionStatus(str, Enum):
    DRAFT = "DRAFT"
    EXTRACTING = "EXTRACTING"
    IN_UNDERWRITING = "IN_UNDERWRITING"
    PENDING_CLIENT_APPROVAL = "PENDING_CLIENT_APPROVAL"
    CHANGES_REQUESTED_CLIENT = "CHANGES_REQUESTED_CLIENT"
    REVALIDATING = "REVALIDATING"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    CLIENT_APPROVED = "CLIENT_APPROVED"
    BILLING_SETUP = "BILLING_SETUP"
    PENDING_BILLING_AUDIT = "PENDING_BILLING_AUDIT"
    ACTIVE = "ACTIVE"


class Stage(str, Enum):
    """The five steps a contract is understood to move through.

    Statuses are the machine's own vocabulary and there are more of them than anyone outside
    it needs — a re-upload being sanity-checked and a customer sitting on a change request are
    both "Review" to everybody but the machine. The stage is what the interface names, what the
    lifecycle page groups by, and what a person means when they ask where a deal has got to.
    """

    NEGOTIATIONS = "NEGOTIATIONS"
    ONBOARDING = "ONBOARDING"
    REVIEW = "REVIEW"
    BILLING_SETUP = "BILLING_SETUP"
    BILLING_AUDIT = "BILLING_AUDIT"
    ACTIVE = "ACTIVE"


STAGE_LABELS: dict[Stage, str] = {
    Stage.NEGOTIATIONS: "Negotiations",
    Stage.ONBOARDING: "Onboarding",
    Stage.REVIEW: "Review",
    Stage.BILLING_SETUP: "Billing Setup",
    Stage.BILLING_AUDIT: "Billing Audit",
    Stage.ACTIVE: "Active",
}

STAGE_BLURBS: dict[Stage, str] = {
    Stage.NEGOTIATIONS: "The deal is being agreed with the customer offline.",
    Stage.ONBOARDING: "Agreements are uploaded and read.",
    Stage.REVIEW: "The customer reviews the terms and signs, or asks for changes.",
    Stage.BILLING_SETUP: "Billing is being configured from the agreed terms.",
    Stage.BILLING_AUDIT: "The billing breakdown is checked before the engagement goes live.",
    Stage.ACTIVE: "Signed, billing and running.",
}


class Action(str, Enum):
    SUBMIT_FOR_PROCESSING = "submit_for_processing"  # analyst kicks off extraction
    PIPELINE_DONE = "pipeline_done"  # system: parse+extract finished
    SUBMIT_TO_CLIENT = "submit_to_client"
    CLIENT_APPROVE = "client_approve"
    CLIENT_REQUEST_CHANGES = "client_request_changes"
    REUPLOAD = "reupload"  # analyst re-uploads a document version
    RESUBMIT_TO_CLIENT = "resubmit_to_client"  # analyst rejects the change request, resubmits
    SANITY_PASS = "sanity_pass"  # system: re-upload passed sanity checks
    SANITY_FAIL = "sanity_fail"  # system: re-upload failed sanity checks
    CAPTURE_FIELDS = "capture_fields"  # system: freeze approved terms, start billing setup
    BILLING_READY = "billing_ready"  # system: billing config generated, ready to be audited
    APPROVE_BILLING = "approve_billing"  # the audit passes and the engagement goes live


# Role sets. PROVIDER is included wherever FINANCE is, because finance is folded into the
# provider group for now (guarded by a single permission).
_PROVIDER = frozenset({Role.PROVIDER})
_CLIENT = frozenset({Role.CLIENT})
_FINANCE = frozenset({Role.FINANCE, Role.PROVIDER})
_SYSTEM = frozenset({Role.SYSTEM})


@dataclass(frozen=True)
class Transition:
    action: Action
    source: SubmissionStatus
    target: SubmissionStatus
    allowed_roles: frozenset[Role]


S = SubmissionStatus
A = Action

TRANSITIONS: tuple[Transition, ...] = (
    Transition(A.SUBMIT_FOR_PROCESSING, S.DRAFT, S.EXTRACTING, _PROVIDER),
    Transition(A.PIPELINE_DONE, S.EXTRACTING, S.IN_UNDERWRITING, _SYSTEM),
    Transition(A.SUBMIT_TO_CLIENT, S.IN_UNDERWRITING, S.PENDING_CLIENT_APPROVAL, _PROVIDER),
    Transition(A.CLIENT_APPROVE, S.PENDING_CLIENT_APPROVAL, S.CLIENT_APPROVED, _CLIENT),
    Transition(
        A.CLIENT_REQUEST_CHANGES, S.PENDING_CLIENT_APPROVAL, S.CHANGES_REQUESTED_CLIENT, _CLIENT
    ),
    Transition(A.REUPLOAD, S.CHANGES_REQUESTED_CLIENT, S.REVALIDATING, _PROVIDER),
    Transition(A.REUPLOAD, S.VALIDATION_FAILED, S.REVALIDATING, _PROVIDER),
    # Replace a wrong / outdated document while still under analyst review.
    Transition(A.REUPLOAD, S.IN_UNDERWRITING, S.REVALIDATING, _PROVIDER),
    Transition(
        A.RESUBMIT_TO_CLIENT, S.CHANGES_REQUESTED_CLIENT, S.PENDING_CLIENT_APPROVAL, _PROVIDER
    ),
    Transition(A.SANITY_PASS, S.REVALIDATING, S.EXTRACTING, _SYSTEM),
    Transition(A.SANITY_FAIL, S.REVALIDATING, S.VALIDATION_FAILED, _SYSTEM),
    # The customer signing is what starts billing setup; nobody presses a button in between.
    Transition(A.CAPTURE_FIELDS, S.CLIENT_APPROVED, S.BILLING_SETUP, _SYSTEM),
    Transition(A.BILLING_READY, S.BILLING_SETUP, S.PENDING_BILLING_AUDIT, _SYSTEM),
    # The audit reads the billing that was actually generated, which is why it sits here and
    # not before setup: approving terms tells you what should be billed, not what will be.
    Transition(A.APPROVE_BILLING, S.PENDING_BILLING_AUDIT, S.ACTIVE, _FINANCE),
)


_BY_KEY: dict[tuple[SubmissionStatus, Action], Transition] = {
    (t.source, t.action): t for t in TRANSITIONS
}

TERMINAL_STATES = frozenset({S.ACTIVE})


class IllegalTransition(Exception):
    """Raised when an (state, action) pair is invalid or the role isn't permitted."""


def _norm_status(value: SubmissionStatus | str) -> SubmissionStatus:
    return value if isinstance(value, SubmissionStatus) else SubmissionStatus(value)


def _norm_action(value: Action | str) -> Action:
    return value if isinstance(value, Action) else Action(value)


def _norm_role(value: Role | str) -> Role:
    return value if isinstance(value, Role) else Role(value)


# Which of the five steps each status belongs to. Every status has exactly one home, and a
# status added without one shows up immediately in the test that walks this map.
STAGE_OF: dict[SubmissionStatus, Stage] = {
    S.DRAFT: Stage.NEGOTIATIONS,
    S.EXTRACTING: Stage.ONBOARDING,
    S.IN_UNDERWRITING: Stage.ONBOARDING,
    S.REVALIDATING: Stage.ONBOARDING,
    S.VALIDATION_FAILED: Stage.ONBOARDING,
    S.PENDING_CLIENT_APPROVAL: Stage.REVIEW,
    S.CHANGES_REQUESTED_CLIENT: Stage.REVIEW,
    S.CLIENT_APPROVED: Stage.REVIEW,
    S.BILLING_SETUP: Stage.BILLING_SETUP,
    S.PENDING_BILLING_AUDIT: Stage.BILLING_AUDIT,
    S.ACTIVE: Stage.ACTIVE,
}


def stage_of(status: SubmissionStatus | str | None) -> Stage:
    """Which step a status sits in. An unknown status is treated as the very beginning."""
    if status is None:
        return Stage.NEGOTIATIONS
    try:
        return STAGE_OF[_norm_status(status)]
    except (KeyError, ValueError):
        return Stage.NEGOTIATIONS


def role_can(current: SubmissionStatus | str, action: Action | str, role: Role | str) -> bool:
    t = _BY_KEY.get((_norm_status(current), _norm_action(action)))
    return t is not None and _norm_role(role) in t.allowed_roles


def transition(
    current: SubmissionStatus | str, action: Action | str, role: Role | str
) -> SubmissionStatus:
    """Return the target status for a legal (current, action, role), else raise."""
    cur, act, rol = _norm_status(current), _norm_action(action), _norm_role(role)
    t = _BY_KEY.get((cur, act))
    if t is None:
        raise IllegalTransition(f"{act.value} is not allowed from {cur.value}")
    if rol not in t.allowed_roles:
        raise IllegalTransition(
            f"role {rol.value} may not perform {act.value} "
            f"(allowed: {sorted(r.value for r in t.allowed_roles)})"
        )
    return t.target


def allowed_actions(current: SubmissionStatus | str, role: Role | str) -> list[Action]:
    cur, rol = _norm_status(current), _norm_role(role)
    return [t.action for t in TRANSITIONS if t.source == cur and rol in t.allowed_roles]
