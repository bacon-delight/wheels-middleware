"""FastAPI dependencies: shared clients, the current principal, and tenancy guards.

The API sits behind an API Gateway HTTP API with a Cognito JWT authorizer, so the token is
already validated upstream and its claims arrive in the Lambda event. We read them from there
rather than re-verifying. A gated dev fallback (ENABLE_DEBUG_AUTH=1 + X-Debug-* headers) lets
the API run locally; tests override `get_principal` directly.
"""

from __future__ import annotations

import os
from functools import lru_cache

from fastapi import Depends, HTTPException, Request

from ..lifecycle.submission_state import Role
from ..store.models import Membership
from ..store.repository import Repository, utcnow
from ..store.s3 import S3Store
from .principal import Principal


@lru_cache(maxsize=1)
def _repo() -> Repository:
    return Repository()


@lru_cache(maxsize=1)
def _s3() -> S3Store:
    return S3Store()


def get_repo() -> Repository:
    return _repo()


def get_s3() -> S3Store:
    return _s3()


def get_principal(request: Request) -> Principal:
    event = request.scope.get("aws.event") or {}
    authorizer = (event.get("requestContext", {}) or {}).get("authorizer", {}) or {}
    claims = (authorizer.get("jwt", {}) or {}).get("claims")
    if claims:
        return Principal.from_claims(claims)

    if os.getenv("ENABLE_DEBUG_AUTH") == "1":
        h = request.headers
        sub = h.get("x-debug-sub")
        if sub:
            groups = [g for g in h.get("x-debug-groups", "").split(",") if g]
            return Principal(user_id=sub, email=h.get("x-debug-email", ""), groups=groups)

    raise HTTPException(status_code=401, detail="unauthenticated")


def membership_dep(
    engagement_id: str,
    principal: Principal = Depends(get_principal),
    repo: Repository = Depends(get_repo),
) -> Membership:
    """Enforce tenancy: clients must be members; providers are org-level and see everything.

    Client users are pinned to the engagement(s) they were invited to. Provider-side (Wheels)
    staff are managed org-wide (see the Users page), so any provider may open any engagement —
    we synthesize a membership for them when they aren't an explicit member.
    """
    m = repo.get_membership(engagement_id, principal.user_id)
    if m is not None:
        return m
    if principal.is_provider:
        name = principal.name
        if not name:
            prof = repo.get_user_profile(principal.user_id)
            name = prof.name if prof else None
        return Membership(
            engagement_id=engagement_id, user_id=principal.user_id, email=principal.email,
            role=principal.group_role, name=name, created_at=utcnow(),
        )
    raise HTTPException(status_code=403, detail="not a member of this engagement")


def require_provider(member: Membership = Depends(membership_dep)) -> Membership:
    if member.role not in (Role.PROVIDER, Role.FINANCE):
        raise HTTPException(status_code=403, detail="provider role required")
    return member


def require_provider_principal(principal: Principal = Depends(get_principal)) -> Principal:
    """Guard org-level (not engagement-scoped) endpoints: Users, finance dashboard."""
    if not principal.is_provider:
        raise HTTPException(status_code=403, detail="provider role required")
    return principal
