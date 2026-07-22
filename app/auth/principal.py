"""The authenticated caller, built from Cognito JWT claims."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..lifecycle.submission_state import Role


@dataclass
class Principal:
    user_id: str  # Cognito sub
    email: str
    groups: list[str] = field(default_factory=list)
    name: str | None = None

    @classmethod
    def from_claims(cls, claims: dict) -> Principal:
        groups = claims.get("cognito:groups")
        if isinstance(groups, str):
            groups = [g for g in groups.replace(",", " ").split() if g]
        elif not isinstance(groups, list):
            groups = []
        return cls(
            user_id=str(claims.get("sub", "")),
            email=str(claims.get("email", "")),
            groups=list(groups),
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
