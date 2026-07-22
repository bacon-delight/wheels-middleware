"""cognito:groups arrives in several shapes depending on the auth path; all must parse."""

from __future__ import annotations

import pytest

from app.auth.principal import Principal


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("[provider]", ["provider"]),  # API Gateway HTTP JWT authorizer flattening
        ("[provider finance]", ["provider", "finance"]),
        ("[provider, finance]", ["provider", "finance"]),
        ("provider", ["provider"]),  # plain string
        ("provider,client", ["provider", "client"]),
        (["provider"], ["provider"]),  # real list (direct-decoded token)
        (None, []),
        ("[]", []),
    ],
)
def test_group_claim_shapes(raw, expected):
    p = Principal.from_claims({"sub": "u1", "email": "a@b.com", "cognito:groups": raw})
    assert p.groups == expected


def test_bracketed_provider_is_recognized_as_provider():
    # The exact bug: a bracketed single group must still count as provider.
    p = Principal.from_claims({"sub": "u1", "email": "a@b.com", "cognito:groups": "[provider]"})
    assert p.is_provider
    assert p.group_role.value == "provider"


def test_no_groups_is_client():
    p = Principal.from_claims({"sub": "u1", "email": "a@b.com"})
    assert not p.is_provider
    assert p.group_role.value == "client"
