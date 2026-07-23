"""Branded, email-client-safe transactional templates for the approval lifecycle.

One template per notification. Each renders to a subject, an HTML body (inline styles only —
no <style> blocks, so email clients render them and Python's str.format is brace-safe), and a
plain-text fallback. `render(key, **ctx)` fills a template and validates that no placeholder
was left unfilled.

Notifications map onto the Submission state machine; `recipients` documents who each one goes
to so the Notify Lambda can fan out to the right party/parties.
"""

from __future__ import annotations

from dataclasses import dataclass

BRAND = "Wheels Contract Intelligence"
BRAND_COLOR = "#0F3D57"
ACCENT = "#127A9B"
FROM_ORG = "Logiforma"


@dataclass
class RenderedEmail:
    subject: str
    html: str
    text: str


# Inner-body fragments keyed by notification. `cta` is (label, url_context_key) or None.
# `recipients` is documentation of the intended audience for the Notify Lambda.
TEMPLATES: dict[str, dict] = {
    "USER_INVITATION": {
        "recipients": ["invited_user"],
        "subject": "You've been invited to {engagement_name} on " + BRAND,
        "heading": "You're invited to {engagement_name}",
        "intro": "Hello {recipient_name},<br><br>{inviter_name} has invited you to collaborate "
        "on <strong>{engagement_name}</strong> in {brand} as a <strong>{role_label}</strong>.",
        "detail": "Sign in with your email (<strong>{recipient_email}</strong>) and this "
        "temporary password. You'll be asked to set a new password on first login:"
        "<br><br><span style=\"font-family:monospace;font-size:18px;letter-spacing:1px;"
        "background:#F1F5F7;padding:8px 14px;border-radius:6px;display:inline-block\">"
        "{temp_password}</span>",
        "cta": ("Sign in", "login_url"),
        "text": "You've been invited to {engagement_name} on {brand} by {inviter_name} as "
        "{role_label}.\nSign in at {login_url} with {recipient_email} and temporary password: "
        "{temp_password}",
    },
    "PROVIDER_INVITATION": {
        "recipients": ["invited_user"],
        "subject": "You've been added to " + BRAND,
        "heading": "You've been added to " + BRAND,
        "intro": "Hello {recipient_name},<br><br>{inviter_name} has added you as a "
        "<strong>Wheels team member</strong> in {brand}. You'll have access to all "
        "engagements, the review workflow, and the finance dashboard.",
        "detail": "Sign in with your email (<strong>{recipient_email}</strong>) and this "
        "temporary password. You'll be asked to set a new password on first login:"
        "<br><br><span style=\"font-family:monospace;font-size:18px;letter-spacing:1px;"
        "background:#F1F5F7;padding:8px 14px;border-radius:6px;display:inline-block\">"
        "{temp_password}</span>",
        "cta": ("Sign in", "login_url"),
        "text": "You've been added to {brand} by {inviter_name} as a Wheels team member.\n"
        "Sign in at {login_url} with {recipient_email} and temporary password: {temp_password}",
    },
    "TERMS_SUBMITTED_TO_CLIENT": {
        "recipients": ["client"],
        "subject": "Action needed: review the billing terms for {engagement_name}",
        "heading": "Your billing terms are ready for review",
        "intro": "Hello {recipient_name},<br><br>{inviter_name} has prepared the extracted "
        "billing terms for <strong>{engagement_name}</strong> and submitted them for your "
        "review (round {round}).",
        "detail": "Please review each service line and either approve the terms or request "
        "changes with your comments.",
        "cta": ("Review terms", "review_url"),
        "text": "The billing terms for {engagement_name} are ready for your review (round "
        "{round}). Review and approve or request changes at {review_url}",
    },
    "CLIENT_CHANGES_REQUESTED": {
        "recipients": ["provider", "analyst"],
        "subject": "{engagement_name}: the client requested changes",
        "heading": "The client requested changes",
        "intro": "Hello {recipient_name},<br><br>The client reviewed <strong>"
        "{engagement_name}</strong> and requested changes (round {round}).",
        "detail": "Their comments:<br><br><em>{comments}</em><br><br>Update the affected "
        "document(s) and re-upload; the platform will re-validate before it returns to the "
        "client.",
        "cta": ("Open engagement", "engagement_url"),
        "text": "The client requested changes on {engagement_name} (round {round}). "
        "Comments: {comments}. Re-upload the document(s) at {engagement_url}",
    },
    "CLIENT_CHANGES_ACK": {
        "recipients": ["client"],
        "subject": "{engagement_name}: we received your change request",
        "heading": "We received your change request",
        "intro": "Hello {recipient_name},<br><br>Thanks — your requested changes to <strong>"
        "{engagement_name}</strong> have been sent to the provider team.",
        "detail": "Your comments:<br><br><em>{comments}</em><br><br>The team will review and get "
        "back to you with an updated agreement or a response. Nothing more is needed from you "
        "right now.",
        "cta": ("View engagement", "engagement_url"),
        "text": "We received your change request for {engagement_name}: {comments}. The provider "
        "team will respond with an update. {engagement_url}",
    },
    "VALIDATION_FAILED": {
        "recipients": ["provider", "analyst"],
        "subject": "{engagement_name}: re-uploaded document failed validation",
        "heading": "Re-uploaded document failed validation",
        "intro": "Hello {recipient_name},<br><br>The document you re-uploaded for <strong>"
        "{engagement_name}</strong> did not pass the sanity checks.",
        "detail": "Issues found:<br><br><em>{reasons}</em><br><br>Please correct and re-upload "
        "the right document for this client.",
        "cta": ("Open engagement", "engagement_url"),
        "text": "The re-uploaded document for {engagement_name} failed validation: {reasons}. "
        "Correct and re-upload at {engagement_url}",
    },
    "CLIENT_APPROVAL_ACK": {
        "recipients": ["client"],
        "subject": "{engagement_name}: thanks — your approval is recorded",
        "heading": "Your approval is recorded",
        "intro": "Hello {recipient_name},<br><br>Thanks for approving the billing terms for "
        "<strong>{engagement_name}</strong>. Your electronic signature has been recorded.",
        "detail": "Our finance team will complete a final validation and set up billing. You'll "
        "get an email when your payment schedule is ready — nothing more is needed from you now.",
        "cta": ("View engagement", "engagement_url"),
        "text": "Thanks for approving the terms for {engagement_name}. Finance will validate and "
        "set up billing; we'll email you when your payment schedule is ready. {engagement_url}",
    },
    "FINANCE_APPROVAL_NEEDED": {
        "recipients": ["finance"],
        "subject": "Finance review needed: {engagement_name}",
        "heading": "Terms are ready for finance validation",
        "intro": "Hello {recipient_name},<br><br>The client has approved the terms for <strong>"
        "{engagement_name}</strong>; they are now captured and ready for your finance validation.",
        "detail": "Review the captured terms and either approve to set up billing, or request "
        "changes.",
        "cta": ("Validate terms", "finance_url"),
        "text": "Client-approved terms for {engagement_name} need finance validation at "
        "{finance_url}",
    },
    "FINANCE_CHANGES_REQUESTED": {
        "recipients": ["provider", "analyst"],
        "subject": "{engagement_name}: finance requested changes",
        "heading": "Finance requested changes",
        "intro": "Hello {recipient_name},<br><br>Finance reviewed <strong>{engagement_name}"
        "</strong> and requested changes before billing can be set up.",
        "detail": "Their comments:<br><br><em>{comments}</em>",
        "cta": ("Open engagement", "engagement_url"),
        "text": "Finance requested changes on {engagement_name}. Comments: {comments}. "
        "Open at {engagement_url}",
    },
    "TERMS_RESUBMITTED": {
        "recipients": ["client"],
        "subject": "{engagement_name}: the provider responded to your request",
        "heading": "The provider responded to your change request",
        "intro": "Hello {recipient_name},<br><br>{inviter_name} reviewed your change request on "
        "<strong>{engagement_name}</strong> and has resubmitted the terms for your approval.",
        "detail": "Their response:<br><br><em>{comments}</em><br><br>Please review the terms again "
        "and approve, or request further changes.",
        "cta": ("Review terms", "review_url"),
        "text": "The provider responded to your change request on {engagement_name}: {comments}. "
        "Review at {review_url}",
    },
    "CHANGES_APPLIED": {
        "recipients": ["client"],
        "subject": "{engagement_name}: your requested changes are being applied",
        "heading": "Your requested changes are being applied",
        "intro": "Hello {recipient_name},<br><br>{inviter_name} is updating <strong>"
        "{engagement_name}</strong> with the changes you requested.",
        "detail": "Their note:<br><br><em>{comments}</em><br><br>You'll be notified when the "
        "revised terms are ready for your review.",
        "cta": ("Open engagement", "engagement_url"),
        "text": "Your requested changes on {engagement_name} are being applied: {comments}. "
        "{engagement_url}",
    },
    "BILLING_ACTIVE_CLIENT": {
        "recipients": ["client"],
        "subject": "Action needed: your billing is active for {engagement_name}",
        "heading": "Your billing is active",
        "intro": "Hello {recipient_name},<br><br>The billing terms for <strong>"
        "{engagement_name}</strong> are fully approved and your account is now active.",
        "detail": "Your payment schedule is ready. Please review it and make your first payment. "
        "You can pay each installment on its due date, or pay the next one early if you prefer.",
        "cta": ("View schedule & pay", "billing_url"),
        "text": "Your billing for {engagement_name} is active. Review your payment schedule and "
        "make your first payment at {billing_url}",
    },
    "BILLING_CONFIGURED_PROVIDER": {
        "recipients": ["provider", "finance"],
        "subject": "{engagement_name}: billing configured — engagement is active",
        "heading": "Billing configured — engagement is active",
        "intro": "Hello {recipient_name},<br><br>The billing configuration for <strong>"
        "{engagement_name}</strong> was generated from the approved terms and the engagement is "
        "now active.",
        "detail": "The client has been sent their payment schedule. You can track payments and "
        "send reminders from the billing page.",
        "cta": ("Open billing", "billing_url"),
        "text": "Billing for {engagement_name} is configured and the engagement is active. "
        "Track payments at {billing_url}",
    },
    "PAYMENT_REMINDER": {
        "recipients": ["client"],
        "subject": "Payment reminder: {period} for {engagement_name}",
        "heading": "A payment reminder",
        "intro": "Hello {recipient_name},<br><br>{sender_name} would like to remind you that your "
        "payment for <strong>{engagement_name}</strong> {status_phrase}.",
        "detail": "<strong>{period}</strong> — {amount}, due {due_date}.<br><br>Please make the "
        "payment at your earliest convenience.",
        "cta": ("Pay now", "billing_url"),
        "text": "Reminder: your payment for {engagement_name} {status_phrase}. {period} — "
        "{amount}, due {due_date}. Pay at {billing_url}",
    },
    "PAYMENT_CONFIRMATION": {
        "recipients": ["client"],
        "subject": "Payment received: {amount} for {engagement_name}",
        "heading": "Payment received — thank you",
        "intro": "Hello {recipient_name},<br><br>We've received your payment for <strong>"
        "{engagement_name}</strong>. Thank you.",
        "detail": "<strong>{period}</strong> — {amount}, due {due_date}. This installment is now "
        "marked as paid.",
        "cta": ("View schedule", "billing_url"),
        "text": "Payment received for {engagement_name}: {period} — {amount}. Thank you. "
        "{billing_url}",
    },
    "PAYMENT_RECEIVED": {
        "recipients": ["provider", "finance"],
        "subject": "{engagement_name}: payment received ({amount})",
        "heading": "A payment was received",
        "intro": "Hello {recipient_name},<br><br>{payer_name} paid an installment for <strong>"
        "{engagement_name}</strong>.",
        "detail": "<strong>{period}</strong> — {amount}, due {due_date}.",
        "cta": ("Open billing", "billing_url"),
        "text": "Payment received for {engagement_name}: {period} — {amount} from {payer_name}. "
        "{billing_url}",
    },
}


def _layout(preheader: str, heading: str, body: str, cta_block: str) -> str:
    return (
        '<div style="margin:0;padding:0;background:#EEF2F4;font-family:-apple-system,Segoe UI,'
        'Roboto,Helvetica,Arial,sans-serif;color:#1B2733">'
        f'<span style="display:none;max-height:0;overflow:hidden;opacity:0">{preheader}</span>'
        '<div style="max-width:560px;margin:0 auto;padding:24px">'
        f'<div style="font-size:13px;font-weight:600;color:{ACCENT};letter-spacing:.5px;'
        f'text-transform:uppercase;padding:4px 0 12px">{BRAND}</div>'
        '<div style="background:#FFFFFF;border-radius:12px;padding:28px 28px 8px;'
        'box-shadow:0 1px 3px rgba(15,61,87,.08)">'
        f'<h1 style="margin:0 0 14px;font-size:20px;line-height:1.3;color:{BRAND_COLOR}">'
        f'{heading}</h1>'
        f'<div style="font-size:15px;line-height:1.6;color:#3A4A57">{body}</div>'
        f'{cta_block}'
        '<div style="border-top:1px solid #EDF1F3;margin-top:24px;padding:16px 0 8px;'
        'font-size:12px;color:#8A98A3">This is an automated message from ' + BRAND +
        f'. Sent by {FROM_ORG}. If this wasn\'t meant for you, please ignore it.</div>'
        '</div></div></div>'
    )


def _cta_block(label: str, url: str) -> str:
    return (
        f'<div style="padding:22px 0 8px"><a href="{url}" '
        f'style="background:{ACCENT};color:#FFFFFF;text-decoration:none;font-weight:600;'
        f'font-size:15px;padding:12px 22px;border-radius:8px;display:inline-block">{label}</a>'
        '</div>'
    )


class _SafeDict(dict):
    def __missing__(self, key):  # surface unfilled placeholders instead of KeyError
        raise KeyError(key)


def render(key: str, **ctx) -> RenderedEmail:
    if key not in TEMPLATES:
        raise KeyError(f"unknown email template: {key}")
    t = TEMPLATES[key]
    ctx.setdefault("brand", BRAND)
    body_parts = [t["intro"]]
    if t.get("detail"):
        body_parts.append('<div style="margin-top:14px">' + t["detail"] + "</div>")
    body = "".join(body_parts)

    cta_block = ""
    if t.get("cta"):
        label, url_key = t["cta"]
        cta_block = _cta_block(label, "{" + url_key + "}")

    html = _layout(t["subject"], t["heading"], body, cta_block)
    try:
        subject = t["subject"].format_map(_SafeDict(ctx))
        html = html.format_map(_SafeDict(ctx))
        text = t["text"].format_map(_SafeDict(ctx))
    except KeyError as e:
        raise KeyError(f"missing context {e} for email template {key!r}") from None
    return RenderedEmail(subject=subject, html=html, text=text)
