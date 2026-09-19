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
    # Signing sets billing up on its own; the next thing a person does is audit the result.
    s = transition(s, Action.CAPTURE_FIELDS, Role.SYSTEM)
    assert s == SubmissionStatus.BILLING_SETUP
    s = transition(s, Action.BILLING_READY, Role.SYSTEM)
    assert s == SubmissionStatus.PENDING_BILLING_AUDIT
    s = transition(s, Action.APPROVE_BILLING, Role.PROVIDER)  # finance folded into provider
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


def test_reupload_allowed_during_underwriting():
    """A wrong / outdated document can be replaced while still under analyst review."""
    s = SubmissionStatus.IN_UNDERWRITING
    assert role_can(s, Action.REUPLOAD, Role.PROVIDER)
    assert not role_can(s, Action.REUPLOAD, Role.CLIENT)
    assert transition(s, Action.REUPLOAD, Role.PROVIDER) == SubmissionStatus.REVALIDATING


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
    assert role_can(SubmissionStatus.PENDING_BILLING_AUDIT, Action.APPROVE_BILLING, Role.PROVIDER)
    assert role_can(SubmissionStatus.PENDING_BILLING_AUDIT, Action.APPROVE_BILLING, Role.FINANCE)
    assert not role_can(
        SubmissionStatus.PENDING_BILLING_AUDIT, Action.APPROVE_BILLING, Role.CLIENT
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


def test_the_audit_has_one_way_out():
    """The audit corrects the billing where it stands, so approving is the only move it makes.

    A "send it back" would mean days of round trip to change a number the auditor is already
    looking at, and it left a status behind that nothing could act on."""
    s = SubmissionStatus.PENDING_BILLING_AUDIT
    assert allowed_actions(s, Role.PROVIDER) == [Action.APPROVE_BILLING]
    assert transition(s, Action.APPROVE_BILLING, Role.PROVIDER) == SubmissionStatus.ACTIVE


def test_every_status_belongs_to_exactly_one_step():
    """A status with no step is invisible on the lifecycle board — it would simply not appear."""
    from app.lifecycle.submission_state import STAGE_OF

    assert set(STAGE_OF) == set(SubmissionStatus)


def test_no_status_is_a_dead_end_except_the_last_one():
    """Every status must have a way forward, or an engagement can arrive somewhere and stop."""
    from app.lifecycle.submission_state import TRANSITIONS

    sources = {t.source for t in TRANSITIONS}
    stuck = [s for s in SubmissionStatus if s not in sources and s is not SubmissionStatus.ACTIVE]
    assert stuck == [], f"these statuses have no exit: {[s.value for s in stuck]}"


def test_the_five_steps_read_the_way_the_product_describes_them():
    """The names are the product's, not the machine's, and the interface takes them from here."""
    from app.lifecycle.submission_state import STAGE_LABELS, Stage

    assert [STAGE_LABELS[s] for s in Stage] == [
        "Negotiations", "Onboarding", "Review", "Billing Setup", "Billing Audit", "Active",
    ]


def test_a_signed_contract_reaches_the_audit_without_anybody_pressing_anything():
    """Steps 3 to 5: the customer signs, billing is generated, a person audits what came out."""
    s = SubmissionStatus.PENDING_CLIENT_APPROVAL
    s = transition(s, Action.CLIENT_APPROVE, Role.CLIENT)
    s = transition(s, Action.CAPTURE_FIELDS, Role.SYSTEM)
    s = transition(s, Action.BILLING_READY, Role.SYSTEM)
    assert s == SubmissionStatus.PENDING_BILLING_AUDIT
    # And the only human action between signing and going live is the audit itself.
    assert allowed_actions(s, Role.PROVIDER) == [Action.APPROVE_BILLING]
