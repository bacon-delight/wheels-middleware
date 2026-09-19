"""Invite a provider- or client-side user.

We drive the whole invite ourselves (AdminCreateUser with the Cognito email SUPPRESSED, then
our own branded SES email), so the invitation is consistent and can send from SES in
ap-south-1 even though the user pool is in ap-south-2.
"""

from __future__ import annotations

import logging
import secrets
import string

from .config import Settings, get_settings
from .lifecycle.submission_state import Role
from .store.models import Engagement, Membership, ProviderUser
from .store.repository import Repository, utcnow

log = logging.getLogger(__name__)


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


def add_existing_member(
    repo: Repository,
    engagement: Engagement,
    person: Membership,
    inviter_name: str,
    settings: Settings | None = None,
) -> Membership:
    """Give someone who already has an account access to another of their engagements.

    No Cognito call at all: the user, their group and their password already exist, and
    `admin_create_user` on an existing username either fails or resets credentials that are in
    use. All that is missing is the membership row and a note that it happened.
    """
    settings = settings or get_settings()
    membership = repo.put_membership(
        Membership(
            engagement_id=engagement.engagement_id,
            user_id=person.user_id,
            email=person.email,
            role=Role.CLIENT,
            name=person.name,
            phone=person.phone,
            created_at=utcnow(),
        )
    )

    from .notify.emailer import Emailer

    # Best-effort, unlike an invitation: the access is the point and it is already granted, and
    # this person can sign in with the password they have. Failing the request over the courtesy
    # email would report "could not add" for someone who had in fact just been added.
    try:
        Emailer(settings).send(
            to=person.email,
            template="ENGAGEMENT_ACCESS_ADDED",
            recipient_name=person.name or person.email,
            recipient_email=person.email,
            engagement_name=engagement.name,
            inviter_name=inviter_name,
            role_label=_role_label(Role.CLIENT),
            login_url=f"{settings.ui_url}/login",
        )
    except Exception as e:  # noqa: BLE001 - the membership stands either way
        log.warning("access-added email -> %s failed: %s", person.email, e)
    return membership


def create_provider_user(
    repo: Repository,
    email: str,
    inviter_name: str,
    name: str | None = None,
    settings: Settings | None = None,
) -> ProviderUser:
    """Create an org-level (Wheels-side) provider user — not tied to any engagement."""
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
        MessageAction="SUPPRESS",
    )
    sub = next(a["Value"] for a in resp["User"]["Attributes"] if a["Name"] == "sub")
    cog.admin_add_user_to_group(
        UserPoolId=settings.cognito_user_pool_id, Username=email, GroupName="provider"
    )
    provider = repo.put_provider_user(
        ProviderUser(user_id=sub, email=email, name=name, created_at=utcnow())
    )

    from .notify.emailer import Emailer

    Emailer(settings).send(
        to=email,
        template="PROVIDER_INVITATION",
        recipient_name=name or email,
        recipient_email=email,
        inviter_name=inviter_name,
        temp_password=temp,
        login_url=f"{settings.ui_url}/login",
    )
    return provider
