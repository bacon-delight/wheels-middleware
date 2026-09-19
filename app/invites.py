"""Invite a provider- or client-side user.

We drive the whole invite ourselves (AdminCreateUser with the Cognito email SUPPRESSED, then
our own branded SES email), so the invitation is consistent and can send from SES in
ap-south-1 even though the user pool is in ap-south-2.
"""

from __future__ import annotations

import logging
import re
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


class AccountIsStaff(Exception):
    """The address belongs to a Wheels account, which is not a customer contact."""


def invite_or_add(
    repo: Repository,
    engagement: Engagement,
    email: str,
    inviter_name: str,
    name: str | None = None,
    settings: Settings | None = None,
) -> tuple[Membership, str]:
    """Invite an address, or give the account behind it access when one already exists.

    "User account already exists" was a dead end: the person typing knows the address is right
    and has no way to act on being told it is taken. An existing account wants the membership,
    not a second account — the same thing the picker does, reached by typing instead.
    """
    settings = settings or get_settings()
    from botocore.exceptions import ClientError

    try:
        return create_and_invite(
            repo, engagement, email, Role.CLIENT, inviter_name, name, settings
        ), "invited"
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "UsernameExistsException":
            raise
        # Held rather than re-raised in place: if the account cannot be resolved, the original
        # collision is still the truest thing we can report.
        taken = e

    account = find_account(email=email, settings=settings)
    if account is None:
        raise taken
    if account["is_provider"]:
        raise AccountIsStaff(account["email"])
    existing = repo.get_membership(engagement.engagement_id, account["user_id"])
    if existing is not None:
        return existing, "already"
    ensure_client_group(account["username"], settings)
    person = Membership(
        engagement_id=engagement.engagement_id, user_id=account["user_id"],
        email=account["email"], role=Role.CLIENT, name=account["name"] or name,
        phone=account["phone"], created_at=utcnow(),
    )
    return add_existing_member(repo, engagement, person, inviter_name, settings), "added"


def _account(cog, user: dict, settings: Settings) -> dict:
    """One Cognito account, flattened to what the picker and the add path need."""
    # admin_get_user returns UserAttributes; list_users returns Attributes. Same data.
    raw = user.get("UserAttributes") or user.get("Attributes", [])
    attrs = {a["Name"]: a["Value"] for a in raw}
    username = user["Username"]
    groups: list[str] = []
    try:
        r = cog.admin_list_groups_for_user(
            UserPoolId=settings.cognito_user_pool_id, Username=username
        )
        groups = [g["GroupName"] for g in r.get("Groups", [])]
    except Exception as e:  # noqa: BLE001 - group membership is advisory here
        log.warning("could not read groups for %s: %s", username, e)
    return {
        "user_id": attrs.get("sub", ""),
        "email": attrs.get("email", username),
        "name": attrs.get("name"),
        "phone": attrs.get("phone_number"),
        "username": username,
        "is_provider": "provider" in groups or "finance" in groups,
    }


# A Cognito sub is a UUID. Anything else is either a synthetic id that only lives in the table
# or someone feeding the pool filter a string of their own — neither is worth a lookup.
_SUB = re.compile(r"^[A-Za-z0-9-]{8,128}$")


def find_account(
    email: str | None = None, sub: str | None = None, settings: Settings | None = None
) -> dict | None:
    """Look an account up in the user pool, by email or by sub.

    The pool is the only place that knows about an account with no membership anywhere — which
    is exactly the account an invitation collides with ("User account already exists"). A
    lookup failure returns None rather than raising: the caller always has another path.
    """
    settings = settings or get_settings()
    if sub is not None and not _SUB.match(sub):
        return None
    import boto3

    try:
        cog = boto3.client("cognito-idp", region_name=settings.core_region)
        if email:
            user = cog.admin_get_user(
                UserPoolId=settings.cognito_user_pool_id, Username=email.strip().lower()
            )
            return _account(cog, user, settings)
        r = cog.list_users(
            UserPoolId=settings.cognito_user_pool_id, Filter=f'sub = "{sub}"', Limit=1
        )
        users = r.get("Users", [])
        return _account(cog, users[0], settings) if users else None
    except Exception as e:  # noqa: BLE001 - "not found" and "no pool here" read the same
        log.info("account lookup (%s) found nothing: %s", email or sub, e)
        return None


def ensure_client_group(username: str, settings: Settings | None = None) -> None:
    """Put an existing account in the client group. Idempotent, and never fatal."""
    settings = settings or get_settings()
    import boto3

    try:
        boto3.client("cognito-idp", region_name=settings.core_region).admin_add_user_to_group(
            UserPoolId=settings.cognito_user_pool_id, Username=username, GroupName="client"
        )
    except Exception as e:  # noqa: BLE001 - the membership is what grants access
        log.warning("could not add %s to the client group: %s", username, e)


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
