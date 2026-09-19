"""Inviting someone who already has an account, against a moto-backed user pool.

The failure this covers was reported from the UI: typing the address of someone who already
had an account returned "UsernameExistsException: User account already exists" and left the
person with nothing to do about it.
"""

from __future__ import annotations

import boto3
import pytest

from app.config import get_settings
from app.lifecycle.submission_state import Role
from app.store.models import Membership
from app.store.repository import utcnow

from .test_api import REGION, _as, ctx  # noqa: F401 - `ctx` is the shared moto fixture


@pytest.fixture
def pool(monkeypatch):
    """A user pool with the two groups the app assigns, wired into settings.

    `get_settings` is lru_cached — one Settings per Lambda container in production — so the
    cache has to be dropped around the environment change, or the pool id never reaches it.
    """
    cog = boto3.client("cognito-idp", region_name=REGION)
    pool_id = cog.create_user_pool(PoolName="wheels-test")["UserPool"]["Id"]
    for group in ("client", "provider"):
        cog.create_group(GroupName=group, UserPoolId=pool_id)
    monkeypatch.setenv("COGNITO_USER_POOL_ID", pool_id)
    monkeypatch.setenv("CORE_REGION", REGION)
    get_settings.cache_clear()
    yield cog, pool_id
    get_settings.cache_clear()


def _account(cog, pool_id, email, name=None, group="client"):
    attrs = [{"Name": "email", "Value": email}]
    if name:
        attrs.append({"Name": "name", "Value": name})
    cog.admin_create_user(
        UserPoolId=pool_id, Username=email, UserAttributes=attrs, MessageAction="SUPPRESS"
    )
    cog.admin_add_user_to_group(UserPoolId=pool_id, Username=email, GroupName=group)
    user = cog.admin_get_user(UserPoolId=pool_id, Username=email)
    return next(a["Value"] for a in user["UserAttributes"] if a["Name"] == "sub")


def _engagement(client, name="Engagement", customer="Apex Pvt Ltd"):
    cid = client.post("/customers", json={"legal_name": customer}).json()["customer"][
        "customer_id"
    ]
    r = client.post("/engagements", json={"name": name, "customer_id": cid})
    return r.json()["engagement"]["engagement_id"], cid


def test_inviting_an_address_that_already_has_an_account_grants_access(ctx, pool, monkeypatch):  # noqa: F811
    client, repo, state = ctx
    cog, pool_id = pool
    sent: list[dict] = []
    monkeypatch.setattr("app.notify.emailer.Emailer.send", lambda self, **kw: sent.append(kw))

    sub = _account(cog, pool_id, "jordan@apex.com", "Jordan Lee")
    eid, _ = _engagement(client)

    r = client.post(f"/engagements/{eid}/invitations", json={"email": "jordan@apex.com"})
    assert r.status_code == 201, r.text
    # Added, not invited: no second account and no new temporary password.
    assert r.json()["outcome"] == "added"
    assert r.json()["membership"]["user_id"] == sub
    assert sent[-1]["template"] == "ENGAGEMENT_ACCESS_ADDED" and "temp_password" not in sent[-1]
    assert repo.get_membership(eid, sub) is not None

    # Doing it twice is a conflict, not a duplicate membership.
    assert (
        client.post(f"/engagements/{eid}/invitations", json={"email": "jordan@apex.com"})
        .status_code == 409
    )


def test_inviting_a_wheels_address_as_a_customer_is_refused(ctx, pool, monkeypatch):  # noqa: F811
    """Staff added as a customer would be demoted to client on that engagement."""
    client, repo, state = ctx
    cog, pool_id = pool
    monkeypatch.setattr("app.notify.emailer.Emailer.send", lambda self, **kw: None)

    _account(cog, pool_id, "ana@wheels.com", "Ana Lyst", group="provider")
    eid, _ = _engagement(client)

    r = client.post(f"/engagements/{eid}/invitations", json={"email": "ana@wheels.com"})
    assert r.status_code == 400
    assert "Wheels team member" in r.json()["detail"]


def test_an_account_on_no_engagement_is_findable_and_addable(ctx, pool, monkeypatch):  # noqa: F811
    """The account that exists only in the pool — invisible to the store, found by address."""
    client, repo, state = ctx
    cog, pool_id = pool
    monkeypatch.setattr("app.notify.emailer.Emailer.send", lambda self, **kw: None)

    sub = _account(cog, pool_id, "priya@apex.com", "Priya Nair")
    eid, _ = _engagement(client)

    # Nothing to suggest: they are on no engagement, so only an address finds them.
    assert client.get(f"/engagements/{eid}/invitations/candidates").json()["candidates"] == []
    found = client.get(
        f"/engagements/{eid}/invitations/candidates", params={"q": "priya@apex.com"}
    ).json()["candidates"]
    assert [c["user_id"] for c in found] == [sub]
    assert found[0]["source"] == "account" and found[0]["engagements"] == []

    assert client.post(f"/engagements/{eid}/members", json={"user_id": sub}).status_code == 201
    assert repo.get_membership(eid, sub).name == "Priya Nair"
    # The pool lookup is exact: a partial address must not half-answer.
    assert client.get(
        f"/engagements/{eid}/invitations/candidates", params={"q": "priya"}
    ).json()["candidates"] == []


def test_search_reaches_other_customers_and_says_whose_they_are(ctx, pool, monkeypatch):  # noqa: F811
    client, repo, state = ctx
    monkeypatch.setattr("app.notify.emailer.Emailer.send", lambda self, **kw: None)

    here, _ = _engagement(client, "Walmart lease", "Walmart Inc")
    there, _ = _engagement(client, "AbbVie services", "AbbVie Inc")
    repo.put_membership(Membership(engagement_id=there, user_id="u-abbvie", email="lee@abbvie.com",
                                   role=Role.CLIENT, name="Lee Ray", created_at=utcnow()))

    assert client.get(f"/engagements/{here}/invitations/candidates").json()["candidates"] == []
    found = client.get(
        f"/engagements/{here}/invitations/candidates", params={"q": "lee"}
    ).json()["candidates"]
    assert found[0]["customers"] == ["AbbVie Inc"] and found[0]["same_customer"] is False
    assert found[0]["engagements"] == ["AbbVie services"]
