"""The submission state machine enforces the approval workflow and its role guards."""

from __future__ import annotations

import pytest

from app.lifecycle.submission_state import (
    Action,
    IllegalTransition,
    Role,
    SubmissionStatus,
    allowed_actions,
    role_can,
    transition,
)


def test_happy_path_to_active():
    """A full round-trip from draft to billing-active with the right actors."""
    s = SubmissionStatus.DRAFT
    s = transition(s, Action.SUBMIT_FOR_PROCESSING, Role.PROVIDER)
    assert s == SubmissionStatus.EXTRACTING
    s = transition(s, Action.PIPELINE_DONE, Role.SYSTEM)
    assert s == SubmissionStatus.IN_UNDERWRITING
    s = transition(s, Action.SUBMIT_TO_CLIENT, Role.PROVIDER)
    assert s == SubmissionStatus.PENDING_CLIENT_APPROVAL
    s = transition(s, Action.CLIENT_APPROVE, Role.CLIENT)
    assert s == SubmissionStatus.CLIENT_APPROVED
    s = transition(s, Action.CAPTURE_FIELDS, Role.SYSTEM)
    assert s == SubmissionStatus.PENDING_FINANCE_APPROVAL
    s = transition(s, Action.FINANCE_APPROVE, Role.PROVIDER)  # finance folded into provider
    assert s == SubmissionStatus.FINANCE_APPROVED
    s = transition(s, Action.SETUP_BILLING, Role.PROVIDER)
    assert s == SubmissionStatus.BILLING_SETUP
    s = transition(s, Action.BILLING_DONE, Role.SYSTEM)
    assert s == SubmissionStatus.ACTIVE


def test_client_request_changes_reupload_loop():
    """Client rejects -> analyst re-uploads -> sanity check -> back into underwriting."""
    s = SubmissionStatus.PENDING_CLIENT_APPROVAL
    s = transition(s, Action.CLIENT_REQUEST_CHANGES, Role.CLIENT)
    assert s == SubmissionStatus.CHANGES_REQUESTED_CLIENT
    s = transition(s, Action.REUPLOAD, Role.PROVIDER)
    assert s == SubmissionStatus.REVALIDATING
    s = transition(s, Action.SANITY_FAIL, Role.SYSTEM)
    assert s == SubmissionStatus.VALIDATION_FAILED
    s = transition(s, Action.REUPLOAD, Role.PROVIDER)  # fix + re-upload
    assert s == SubmissionStatus.REVALIDATING
    s = transition(s, Action.SANITY_PASS, Role.SYSTEM)
    assert s == SubmissionStatus.EXTRACTING


def test_illegal_transition_rejected():
    with pytest.raises(IllegalTransition):
        transition(SubmissionStatus.DRAFT, Action.CLIENT_APPROVE, Role.CLIENT)


def test_client_cannot_perform_provider_action():
    with pytest.raises(IllegalTransition):
        transition(SubmissionStatus.IN_UNDERWRITING, Action.SUBMIT_TO_CLIENT, Role.CLIENT)


def test_provider_cannot_approve_on_behalf_of_client():
    with pytest.raises(IllegalTransition):
        transition(SubmissionStatus.PENDING_CLIENT_APPROVAL, Action.CLIENT_APPROVE, Role.PROVIDER)


def test_finance_action_permitted_for_provider_but_not_client():
    assert role_can(SubmissionStatus.PENDING_FINANCE_APPROVAL, Action.FINANCE_APPROVE, Role.PROVIDER)
    assert role_can(SubmissionStatus.PENDING_FINANCE_APPROVAL, Action.FINANCE_APPROVE, Role.FINANCE)
    assert not role_can(
        SubmissionStatus.PENDING_FINANCE_APPROVAL, Action.FINANCE_APPROVE, Role.CLIENT
    )


def test_allowed_actions_are_role_scoped():
    at_client_gate = SubmissionStatus.PENDING_CLIENT_APPROVAL
    assert set(allowed_actions(at_client_gate, Role.CLIENT)) == {
        Action.CLIENT_APPROVE,
        Action.CLIENT_REQUEST_CHANGES,
    }
    # The provider has nothing to do while it's the client's turn.
    assert allowed_actions(at_client_gate, Role.PROVIDER) == []


def test_string_inputs_are_accepted():
    assert transition("DRAFT", "submit_for_processing", "provider") == SubmissionStatus.EXTRACTING
