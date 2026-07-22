"""Submission approval lifecycle: the persisted state machine and its guards."""

from .submission_state import (
    Action,
    IllegalTransition,
    Role,
    SubmissionStatus,
    allowed_actions,
    role_can,
    transition,
)

__all__ = [
    "Action",
    "IllegalTransition",
    "Role",
    "SubmissionStatus",
    "allowed_actions",
    "role_can",
    "transition",
]
