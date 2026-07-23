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
    PENDING_FINANCE_APPROVAL = "PENDING_FINANCE_APPROVAL"
    CHANGES_REQUESTED_FINANCE = "CHANGES_REQUESTED_FINANCE"
    FINANCE_APPROVED = "FINANCE_APPROVED"
    BILLING_SETUP = "BILLING_SETUP"
    ACTIVE = "ACTIVE"


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
    CAPTURE_FIELDS = "capture_fields"  # system: freeze approved terms snapshot
    FINANCE_APPROVE = "finance_approve"
    FINANCE_REQUEST_CHANGES = "finance_request_changes"
    REOPEN = "reopen"  # analyst reopens after finance changes
    SETUP_BILLING = "setup_billing"
    BILLING_DONE = "billing_done"  # system: billing config generated


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
    Transition(A.CAPTURE_FIELDS, S.CLIENT_APPROVED, S.PENDING_FINANCE_APPROVAL, _SYSTEM),
    Transition(A.FINANCE_APPROVE, S.PENDING_FINANCE_APPROVAL, S.FINANCE_APPROVED, _FINANCE),
    Transition(
        A.FINANCE_REQUEST_CHANGES,
        S.PENDING_FINANCE_APPROVAL,
        S.CHANGES_REQUESTED_FINANCE,
        _FINANCE,
    ),
    Transition(A.REOPEN, S.CHANGES_REQUESTED_FINANCE, S.IN_UNDERWRITING, _PROVIDER),
    Transition(A.SETUP_BILLING, S.FINANCE_APPROVED, S.BILLING_SETUP, _FINANCE),
    Transition(A.BILLING_DONE, S.BILLING_SETUP, S.ACTIVE, _SYSTEM),
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
