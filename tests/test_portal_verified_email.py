"""Only an email the identity provider verified decides MCP access and the CMDB owner (final review of 0.6.0): a self-asserted one
counts as no email, so the person sees no MCP servers and their app's CI is cmdb_owner_unknown."""

from urllib.parse import parse_qs, urlsplit

import pytest
from portal_fakes import T0
from test_portal_collector_cmdb import Spy, mock_with, row, run

from portal.cloudflare_mock import load_mock

EMAIL = "mwdavisii@example.com"  # the mock Cloudflare's user who may use notes, and a mock ServiceNow user


def sign_in(portal, sub, **claims):
    client = portal.client()
    location = client.get("/login").headers["location"]
    code = portal.fakes["idp"].authorize(location, sub=sub, groups=[], email=EMAIL, **claims)
    state = parse_qs(urlsplit(location).query)["state"][0]
    assert client.get(f"/auth/callback?code={code}&state={state}").status_code == 303
    return next(p for p in portal.store.list_people() if p.subject == sub)


@pytest.mark.parametrize("verified", [False, None, "true"])
def test_an_unverified_email_is_not_kept_so_it_sees_no_mcp_server_and_its_app_is_owner_unknown(portal, verified):
    person = sign_in(portal, "unverified", email_verified=verified)
    assert person.email is None and portal.store.person_email(person.id) is None
    assert load_mock().servers_for(person.email) == frozenset()
    portal.store.intake_observed = lambda: [row(owner=person.id)]
    result = run(portal.store, Spy(mock_with()), now=T0)
    assert result["owner_unknown"] == 1 and result["synced"] == 0 and portal.store.cmdb_record("acme/notes").last_error == "cmdb_owner_unknown"


def test_a_verified_email_is_kept_and_decides_both(portal):
    person = sign_in(portal, "verified", email_verified=True)
    assert person.email == EMAIL and "notes" in load_mock().servers_for(person.email)
    portal.store.intake_observed = lambda: [row(owner=person.id)]
    assert run(portal.store, Spy(mock_with()), now=T0)["synced"] == 1


def test_a_later_unverified_sign_in_drops_the_email_it_had(portal):
    person = sign_in(portal, "s", email_verified=True)
    assert person.email == EMAIL
    assert sign_in(portal, "s", email_verified=False).email is None
