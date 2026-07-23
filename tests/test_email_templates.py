"""Every lifecycle email renders fully, for both parties, with no unfilled placeholders."""

from __future__ import annotations

import re

import pytest

from app.notify.templates import TEMPLATES, render

# A superset of context available to any template; render() only consumes what it needs.
SAMPLE_CTX = {
    "engagement_name": "Apex Field Services",
    "recipient_name": "Jordan Lee",
    "recipient_email": "jordan@apexfield.example",
    "inviter_name": "Sam Rivera",
    "role_label": "Client reviewer",
    "temp_password": "Tmp-9F2xQ7",
    "login_url": "https://wheels.logiforma.dev/login",
    "review_url": "https://wheels.logiforma.dev/e/apex/review",
    "engagement_url": "https://wheels.logiforma.dev/e/apex",
    "finance_url": "https://wheels.logiforma.dev/e/apex/finance",
    "billing_url": "https://wheels.logiforma.dev/e/apex/billing",
    "round": 2,
    "comments": "Please reduce the maintenance management fee to $10.50.",
    "reasons": "Uploaded document is an MSA, but this slot expects the MLA.",
    # Payment emails
    "amount": "$400.00",
    "period": "March 2026",
    "due_date": "15 Mar 2026",
    "payer_name": "Jordan Lee",
    "sender_name": "Sam Rivera",
    "status_phrase": "is overdue",
}

_PLACEHOLDER = re.compile(r"\{[a-z_]+\}")


@pytest.mark.parametrize("key", list(TEMPLATES.keys()))
def test_template_renders_completely(key):
    email = render(key, **SAMPLE_CTX)
    assert email.subject and email.html and email.text
    # No placeholder like {engagement_name} may survive rendering, in any part.
    for part_name, part in (("subject", email.subject), ("html", email.html), ("text", email.text)):
        leftover = _PLACEHOLDER.findall(part)
        assert not leftover, f"{key} {part_name} has unfilled placeholders: {leftover}"
    assert "Wheels Contract Intelligence" in email.html


def test_every_template_documents_recipients():
    for key, t in TEMPLATES.items():
        assert t.get("recipients"), f"{key} must document its recipients"


def test_missing_context_is_reported_clearly():
    with pytest.raises(KeyError):
        render("USER_INVITATION")  # no context at all
