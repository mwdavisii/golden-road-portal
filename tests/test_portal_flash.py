"""Toasts (spec 2026-10-30 R9): after a POST redirects, the next page shows a one-line confirmation, once. The value is a key from a
fixed table in a short-lived cookie (decision 10), never text from a request."""

import pytest

from portal.web.flash import FLASH_COOKIE, MESSAGES


def toast(text):
    return [line for line in text.splitlines() if 'class="toast"' in line]


def test_a_connection_request_shows_its_toast_once(portal):
    client = portal.client("owner")
    response = client.post("/apps/acme/notes/azure-identity", data={"csrf_token": portal.csrfs["owner"], "principal_id": "sp-notes"})
    assert response.status_code == 303 and FLASH_COOKIE in response.headers["set-cookie"]
    assert toast(client.get(response.headers["location"]).text) == ['<p class="toast" role="status">Connection requested</p>']
    assert toast(client.get(response.headers["location"]).text) == []   # shown once


def test_a_registration_and_a_decision_show_theirs(portal):
    owner = portal.client("owner")
    response = owner.post("/register", data={"csrf_token": portal.csrfs["owner"], "repo": "acme/tools",
                                             "purpose": "Builds internal tools; no email.", "data_classification": "public"})
    assert "Registration submitted for review" in owner.get(response.headers["location"]).text
    admin = portal.client("admin")
    response = admin.post(f"/admin/requests/{portal.seeds['request']}/send-back",
                          data={"csrf_token": portal.csrfs["admin"], "justification": "Say what it does not do."})
    assert response.status_code == 303 and "Sent back to the requester" in admin.get(response.headers["location"]).text


def test_a_removal_shows_removal_requested(portal):
    from test_portal_apps import link_identity_and_merge
    link_identity_and_merge(portal)
    client = portal.client("owner")
    response = client.post("/apps/acme/notes/remove", data={"csrf_token": portal.csrfs["owner"], "what": "azure-identity", "confirmed": "yes"})
    assert "Removal requested" in client.get(response.headers["location"]).text


@pytest.mark.parametrize("value", ["nope", "Removal requested", "%3Cscript%3Ealert(1)%3C%2Fscript%3E", ""])
def test_an_unknown_value_shows_nothing_and_is_cleared(portal, value):
    client = portal.client("owner")
    client.cookies.set(FLASH_COOKIE, value, domain="portal.example.test")
    response = client.get("/me")
    assert toast(response.text) == [] and "alert" not in response.text   # only a key from the table is ever shown
    assert FLASH_COOKIE in response.headers.get("set-cookie", "")   # cleared


def test_the_table_is_fixed_text():
    assert MESSAGES["removal"] == "Removal requested" and MESSAGES["connection"] == "Connection requested"


def test_a_page_rendered_for_a_post_shows_no_toast_and_keeps_it_for_the_next_get(portal):
    client = portal.client("owner")
    client.cookies.set(FLASH_COOKIE, "connection", domain="portal.example.test")
    refused = client.post("/register", data={"csrf_token": portal.csrfs["owner"], "repo": "acme/tools", "purpose": "short",
                                             "data_classification": "public"})
    assert refused.status_code == 400 and toast(refused.text) == [] and FLASH_COOKIE not in refused.headers.get("set-cookie", "")
    assert toast(client.get("/me").text) == ['<p class="toast" role="status">Connection requested</p>']
