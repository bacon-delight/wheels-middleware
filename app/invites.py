"""Invite a provider- or client-side user.

We drive the whole invite ourselves (AdminCreateUser with the Cognito email SUPPRESSED, then
our own branded SES email), so the invitation is consistent and can send from SES in
ap-south-1 even though the user pool is in ap-south-2.
"""

from __future__ import annotations

import secrets
import string

from .config import Settings, get_settings
from .lifecycle.submission_state import Role
from .store.models import Engagement, Membership
from .store.repository import Repository, utcnow


def _temp_password() -> str:
    # Satisfies the pool policy: length >= 12, upper/lower/digit/symbol.
    alphabet = string.ascii_letters + string.digits
    core = "".join(secrets.choice(alphabet) for _ in range(12))
    return f"Wl-{core}9!"


def _role_label(role: Role) -> str:
    return {
        Role.CLIENT: "Client reviewer",
        Role.PROVIDER: "Analyst",
        Role.FINANCE: "Finance approver",
    }.get(role, "User")


def create_and_invite(
    repo: Repository,
    engagement: Engagement,
    email: str,
    role: Role,
    inviter_name: str,
    name: str | None = None,
    settings: Settings | None = None,
) -> Membership:
    settings = settings or get_settings()
    import boto3

    cog = boto3.client("cognito-idp", region_name=settings.core_region)
    temp = _temp_password()

    attrs = [
        {"Name": "email", "Value": email},
        {"Name": "email_verified", "Value": "true"},
    ]
    if name:
        attrs.append({"Name": "name", "Value": name})

    resp = cog.admin_create_user(
        UserPoolId=settings.cognito_user_pool_id,
        Username=email,
        UserAttributes=attrs,
        TemporaryPassword=temp,
        MessageAction="SUPPRESS",  # we send our own branded invite
    )
    sub = next(a["Value"] for a in resp["User"]["Attributes"] if a["Name"] == "sub")

    group = "client" if role == Role.CLIENT else "provider"
    cog.admin_add_user_to_group(
        UserPoolId=settings.cognito_user_pool_id, Username=email, GroupName=group
    )

    membership = repo.put_membership(
        Membership(
            engagement_id=engagement.engagement_id, user_id=sub, email=email,
            role=role, name=name, created_at=utcnow(),
        )
    )

    from .notify.emailer import Emailer

    Emailer(settings).send(
        to=email,
        template="USER_INVITATION",
        recipient_name=name or email,
        recipient_email=email,
        engagement_name=engagement.name,
        inviter_name=inviter_name,
        role_label=_role_label(role),
        temp_password=temp,
        login_url=f"{settings.ui_url}/login",
    )
    return membership
