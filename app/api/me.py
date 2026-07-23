"""Current-user profile + first-login onboarding (mandatory name + phone)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth.deps import get_principal, get_repo
from ..auth.principal import Principal
from ..config import get_settings
from ..store.models import ProviderUser, UserProfile
from ..store.repository import Repository, utcnow

router = APIRouter(tags=["me"])


class ProfileIn(BaseModel):
    name: str
    phone: str


def _is_onboarded(p: UserProfile | None) -> bool:
    return bool(p and p.onboarded and p.name and p.phone)


@router.get("/me")
def get_me(
    principal: Principal = Depends(get_principal),
    repo: Repository = Depends(get_repo),
):
    profile = repo.get_user_profile(principal.user_id)
    return {
        "user_id": principal.user_id,
        "email": principal.email,
        "name": (profile.name if profile else None) or principal.name,
        "phone": profile.phone if profile else None,
        "role": "provider" if principal.is_provider else "client",
        "needs_onboarding": not _is_onboarded(profile),
    }


@router.patch("/me")
def update_me(
    body: ProfileIn,
    principal: Principal = Depends(get_principal),
    repo: Repository = Depends(get_repo),
):
    name, phone = body.name.strip(), body.phone.strip()
    if not name or not phone:
        raise HTTPException(400, "full name and phone are required")
    profile = UserProfile(
        user_id=principal.user_id, email=principal.email, name=name, phone=phone,
        onboarded=True, updated_at=utcnow(),
    )
    repo.put_user_profile(profile)
    # Denormalize onto the user's memberships so People pages show name + phone.
    for m in repo.list_user_memberships(principal.user_id):
        m.name = name
        m.phone = phone
        repo.put_membership(m)
    # Provider staff: keep the org directory + the Cognito name attribute (JWT claim) in sync.
    if principal.is_provider:
        existing = repo.get_provider_user(principal.user_id)
        repo.put_provider_user(
            ProviderUser(
                user_id=principal.user_id, email=principal.email, name=name, phone=phone,
                onboarded=True, created_at=existing.created_at if existing else utcnow(),
            )
        )
    _sync_cognito_name(principal.email, name)
    return {"ok": True, "profile": profile}


def _sync_cognito_name(username: str, name: str) -> None:
    """Best-effort: set the Cognito `name` attribute so future JWTs carry it."""
    try:
        import boto3

        settings = get_settings()
        boto3.client("cognito-idp", region_name=settings.core_region).admin_update_user_attributes(
            UserPoolId=settings.cognito_user_pool_id,
            Username=username,
            UserAttributes=[{"Name": "name", "Value": name}],
        )
    except Exception:  # noqa: BLE001 - non-fatal; the profile is the source of truth
        pass
