"""Authentication + tenancy: principal from Cognito claims, membership/role guards."""

from .deps import get_principal, get_repo, get_s3, membership_dep, require_provider
from .principal import Principal

__all__ = [
    "Principal",
    "get_principal",
    "get_repo",
    "get_s3",
    "membership_dep",
    "require_provider",
]
