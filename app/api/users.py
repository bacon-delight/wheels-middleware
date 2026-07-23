"""Org-level (Wheels-side) provider user management — the sidebar "Users" page.

Providers are org staff, not scoped to an engagement, so they live in a directory partition.
The list unions the directory with any provider/finance memberships (to surface staff created
before the directory existed, e.g. engagement creators).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth.deps import get_repo, require_provider_principal
from ..auth.principal import Principal
from ..store.models import ProviderUser
from ..store.repository import Repository

router = APIRouter(tags=["users"])


class AddProviderIn(BaseModel):
    email: str
    name: str | None = None


def _list_providers(repo: Repository) -> list[ProviderUser]:
    by_id: dict[str, ProviderUser] = {}
    for m in repo.list_all_provider_memberships():
        by_id[m.user_id] = ProviderUser(
            user_id=m.user_id, email=m.email, name=m.name, phone=m.phone,
            onboarded=bool(m.name), created_at=m.created_at,
        )
    # Directory entries are canonical — they win over membership-derived rows.
    for pu in repo.list_provider_directory():
        by_id[pu.user_id] = pu
    return sorted(by_id.values(), key=lambda p: (p.name or p.email).lower())


@router.get("/users")
def list_users(
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    return {"users": _list_providers(repo)}


@router.post("/users", status_code=201)
def add_user(
    body: AddProviderIn,
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    email = body.email.strip().lower()
    if not email:
        raise HTTPException(400, "email is required")
    from ..invites import create_provider_user

    try:
        provider = create_provider_user(
            repo, email, inviter_name=principal.name or principal.email, name=body.name,
        )
    except Exception as e:  # noqa: BLE001 - surface Cognito/SES failures as a clean 400
        raise HTTPException(400, f"could not add provider: {e}") from e
    return {"user": provider}
