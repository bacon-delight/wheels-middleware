"""The authenticated caller, built from Cognito JWT claims."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..lifecycle.submission_state import Role


def _parse_groups(raw) -> list[str]:
    """Normalize the cognito:groups claim to a list.

    The API Gateway HTTP JWT authorizer flattens the array to a string like "[provider]" or
    "[provider finance]"; direct-decoded tokens give a real list; other setups give
    "provider" or "provider,finance". Handle all of them.
    """
    if isinstance(raw, list):
        items = [str(g) for g in raw]
    elif isinstance(raw, str):
        items = raw.strip().strip("[]").replace(",", " ").split()
    else:
        items = []
    return [g.strip().strip('"').strip("'") for g in items if g.strip().strip('"').strip("'")]


@dataclass
class Principal:
    user_id: str  # Cognito sub
    email: str
    groups: list[str] = field(default_factory=list)
    name: str | None = None

    @classmethod
    def from_claims(cls, claims: dict) -> Principal:
        groups = _parse_groups(claims.get("cognito:groups"))
        return cls(
            user_id=str(claims.get("sub", "")),
            email=str(claims.get("email", "")),
            groups=groups,
            name=claims.get("name"),
        )

    @property
    def is_provider(self) -> bool:
        return "provider" in self.groups or "finance" in self.groups

    @property
    def group_role(self) -> Role:
        if "provider" in self.groups:
            return Role.PROVIDER
        if "finance" in self.groups:
            return Role.FINANCE
        return Role.CLIENT
