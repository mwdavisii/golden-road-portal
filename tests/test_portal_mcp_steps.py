"""The MCP server picker in steps (spec 2026-10-31 Part 1, "The portal"): Environment (test or prod, each with its gateway), Server (the
gateway's servers the person may use, the ones an app can't use greyed with the reason), Review and save. Each step is a GET carrying its
choices; every step checks the earlier ones again; only the final POST creates the binding request, and it checks everything again.

The mock Cloudflare's policy gives mwdavisii@example.com's servers to owner@example.test (the portal fixture's owner): notes (both
gateways, ready), tickets (on_behalf: no app can use it), docs-search (test, status error). finance-ledger (prod) is other@example.com's."""

import html
import json
import re

import httpx
import pytest
from portal_fakes import make_settings, mock_cloudflare
from test_portal_apps import bindings, merge_next, post

from portal.cloudflare_mock import MOCK, MockCloudflare
from portal.web.flash import FLASH_COOKIE

NEW = "/apps/acme/notes/mcp/new"
ON_BEHALF = "This server needs each person's own sign-in, so an app can't use it."


def with_cloudflare(portal, cloudflare=None, **settings):
    portal.app.state.settings = make_settings(directory=portal.settings.directory, cloudflare=cloudflare or mock_cloudflare(), **settings)
    return portal


@pytest.fixture
def mcp(portal):
    return with_cloudflare(portal)


def get(portal, who="owner", **params):
    return portal.client(who).get(NEW, params=params)


def radios(text, name):
    return set(re.findall(rf'<input type="radio" name="{name}" value="([^"]+)"', text))


def step_of(text):
    return re.search(r'aria-current="step"><span class="step-dot" aria-hidden="true">(\d)</span>', text).group(1)


def add(portal, server_id="notes", who="owner", **form):
    return post(portal, who, "/apps/acme/notes/mcp", server_id=server_id, **form)


def add_and_merge(portal, server_id="notes", **form):
    assert add(portal, server_id, **form).status_code == 303
    portal.clock.advance(seconds=1)
    return merge_next(portal)


def many_servers(n=21):
    doc = json.loads(MOCK.read_text())
    template = doc["portals"][0]["servers"][0]
    doc["portals"][0]["servers"] += [{**template, "server_id": f"s{i:02d}", "name": f"Server {i}"} for i in range(n)]
    doc["users"][0]["servers"] += [f"s{i:02d}" for i in range(n)]
    return mock_cloudflare(doc=doc)


def test_the_section_is_hidden_and_the_steps_are_404_without_cloudflare(portal):
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "MCP servers (Cloudflare gateway)" not in page and "/mcp/new" not in page and "CF_ACCESS_CLIENT_ID" not in page
    assert get(portal).status_code == 404 and get(portal, environment="test").status_code == 404
    response = add(portal)
    assert response.status_code == 400 and "Cloudflare is not set up here." in response.text and not bindings(portal)


def test_the_app_page_shows_the_gateway_and_links_to_the_steps(mcp):
    page = mcp.client("owner").get("/apps/acme/notes").text
    assert "MCP servers (Cloudflare gateway)" in page and "No MCP servers in test yet." in page
    assert '<span class="mono">mcp-test.example.com</span>: one shared gateway per environment' in page
    assert '<a class="button" href="/apps/acme/notes/mcp/new?environment=test">Add an MCP server</a>' in page
    prod = mcp.client("owner").get("/apps/acme/notes", params={"environment": "prod"}).text
    assert '<span class="mono">mcp.example.com</span>: one shared gateway per environment' in prod


def test_step_1_offers_both_environments_with_their_gateways_and_the_tab_pre_chosen(mcp):
    text = get(mcp).text
    assert step_of(text) == "1" and radios(text, "environment") == {"test", "prod"}
    assert "mcp-test.example.com" in text and "mcp.example.com" in text and 'value="test" required checked>' in text
    assert 'value="prod" required checked>' in get(mcp, tab="prod").text


def test_step_2_lists_the_person_s_ready_servers_and_greys_the_ones_an_app_can_t_use(mcp):
    text = get(mcp, environment="test").text
    assert step_of(text) == "2" and radios(text, "server_id") == {"notes"}
    assert "Notes" in text and "Search and write team notes." in text and "search_notes, create_note" in text
    assert html.unescape(text).count(ON_BEHALF) == 1 and '<div class="choice unavailable"><span class="choice-title">Tickets</span>' in text
    assert "Docs search" not in text and "Finance ledger" not in text   # not ready; not the person's
    prod = get(mcp, environment="prod").text
    assert radios(prod, "server_id") == {"notes"} and "Tickets" in prod and "Finance ledger" not in prod and "Docs search" not in prod


def test_the_policy_is_keyed_by_the_person_s_email(mcp):
    doc = json.loads(MOCK.read_text())
    doc["users"] = [{"email": "owner@example.test", "servers": ["finance-ledger"]}]
    with_cloudflare(mcp, mock_cloudflare(doc=doc))
    assert radios(get(mcp, environment="prod").text, "server_id") == {"finance-ledger"}
    text = get(mcp, environment="test").text
    assert radios(text, "server_id") == set() and "No server on this gateway is one you may use." in text


def test_a_bound_server_is_marked_and_not_offered_again(mcp):
    add_and_merge(mcp)
    text = get(mcp, environment="test").text
    assert radios(text, "server_id") == set() and "Already on this app" in text


@pytest.mark.parametrize("params, message, step", [
    ({"server_id": "notes"}, "Choose an environment first.", "1"),
    ({"environment": "test", "server_id": "Notes"}, "Choose a server from the list.", "2"),
    ({"environment": "test", "server_id": "tickets"}, ON_BEHALF, "2"),
    ({"environment": "test", "server_id": "docs-search"}, "That server is not ready in Cloudflare.", "2"),
    ({"environment": "test", "server_id": "finance-ledger"}, "That server is not on the test gateway.", "2"),
    ({"environment": "prod", "server_id": "finance-ledger"}, "You can't use that server in Cloudflare.", "2"),
])
def test_a_skipped_or_tampered_choice_is_refused(mcp, params, message, step):
    response = get(mcp, **params)
    assert response.status_code == 400 and message in html.unescape(response.text) and step_of(response.text) == step
    assert not bindings(mcp)


def test_a_bad_environment_is_a_400(mcp):
    assert get(mcp, environment="staging").status_code == 400 and get(mcp, tab="staging").status_code == 400


def test_step_3_reviews_what_the_container_gets_and_only_the_post_creates_the_request(mcp):
    text = get(mcp, environment="test", server_id="notes").text
    assert step_of(text) == "3" and not bindings(mcp)
    assert "https://mcp-test.example.com/mcp" in text and "CF_ACCESS_CLIENT_ID" in text and "CF_ACCESS_CLIENT_SECRET" in text
    assert html.unescape(text).count("from Secret Server: the app's Cloudflare token") == 2 and "cf-mcp-acme--notes-test" in text
    for name, value in (("environment", "test"), ("server_id", "notes")):
        assert f'<input type="hidden" name="{name}" value="{value}">' in text
    response = add(mcp)
    assert response.status_code == 303 and response.headers["location"] == "/apps/acme/notes?environment=test"
    assert any(c.startswith(f"{FLASH_COOKIE}=mcp_server") for c in response.headers.get_list("set-cookie"))
    [req] = bindings(mcp)
    assert req.state == "approved" and req.final["mcp"] == {"gateway": "mcp-test.example.com", "servers": [
        {"server_id": "notes", "name": "Notes", "verified_by": "mock-cloudflare"}]}


def test_the_app_page_shows_the_server_and_what_the_container_gets(mcp):
    add_and_merge(mcp)
    page = mcp.client("owner").get("/apps/acme/notes").text
    assert '<td>Notes</td><td class="mono">notes</td>' in page and 'confirm_mcp=notes' in page
    assert '<td class="mono">MCP_URL</td><td class="mono">https://mcp-test.example.com/mcp</td>' in page
    assert '<td class="mono">MCP_SERVERS</td><td class="mono">notes</td>' in page
    assert html.unescape(page).count("from Secret Server: the app's Cloudflare token") == 2
    assert 'Egress on 443 to: <span class="mono">mcp-test.example.com</span>.' in page   # no identity needed
    assert "mcp-test.example.com" not in mcp.client("owner").get("/apps/acme/notes", params={"environment": "prod"}).text.split(
        "What the container gets")[1]


@pytest.mark.parametrize("server_id, environment, message", [
    ("tickets", "test", ON_BEHALF), ("docs-search", "test", "not ready"), ("finance-ledger", "prod", "can't use that server"),
    ("finance-ledger", "test", "not on the test gateway"), ("", "test", "Choose a server from the list."),
    ("NOTES", "test", "Choose a server from the list."), ("nope", "test", "not on the test gateway")])
def test_crafted_posts_are_refused_and_nothing_is_stored(mcp, server_id, environment, message):
    response = add(mcp, server_id, environment=environment)
    assert response.status_code == 400 and message in html.unescape(response.text) and not bindings(mcp)


def test_a_duplicate_and_a_21st_server_are_refused(portal):
    with_cloudflare(portal, many_servers())
    add_and_merge(portal)
    response = add(portal)
    assert response.status_code == 400 and "notes is already on this app in test." in response.text and len(bindings(portal)) == 1
    for i in range(19):
        add_and_merge(portal, f"s{i:02d}")
    assert len(bindings(portal)[0].final["mcp"]["servers"]) == 20
    response = add(portal, "s19")
    assert response.status_code == 400 and "at most 20 MCP servers" in response.text and len(bindings(portal)) == 20


def test_removing_a_server_asks_first(mcp):
    add_and_merge(mcp)
    page = mcp.client("owner").get("/apps/acme/notes").text
    assert 'href="/apps/acme/notes?environment=test&amp;confirm_mcp=notes"' in page and 'action="/apps/acme/notes/mcp/remove"' not in page
    ask = mcp.client("owner").get("/apps/acme/notes", params={"confirm_mcp": "notes"})
    assert ask.status_code == 200 and "Remove the MCP server Notes?" in ask.text and "reconciles cloudflare-access.yaml" in ask.text
    assert 'action="/apps/acme/notes/mcp/remove"' in ask.text and 'name="server_id" value="notes"' in ask.text
    assert mcp.client("owner").get("/apps/acme/notes", params={"confirm_mcp": "tickets"}).status_code == 400
    refused = post(mcp, "owner", "/apps/acme/notes/mcp/remove", server_id="notes")
    assert refused.status_code == 400 and "Confirm the removal first" in refused.text and len(bindings(mcp)) == 1
    assert post(mcp, "owner", "/apps/acme/notes/mcp/remove", server_id="tickets", confirmed="yes").status_code == 400
    response = post(mcp, "owner", "/apps/acme/notes/mcp/remove", server_id="notes", confirmed="yes")
    assert response.status_code == 303 and any(c.startswith(f"{FLASH_COOKIE}=removal") for c in response.headers.get_list("set-cookie"))
    assert bindings(mcp)[0].final["mcp"] == {"gateway": "mcp-test.example.com", "servers": []}


def test_no_page_shows_the_cloudflare_token(mcp):
    add_and_merge(mcp)
    pages = [mcp.client("owner").get("/apps/acme/notes").text, mcp.client("admin").get("/apps/acme/notes").text,
             get(mcp).text, get(mcp, environment="test").text, get(mcp, environment="prod").text,
             get(mcp, environment="prod", server_id="notes").text, add(mcp, "tickets").text]
    for text in pages:
        assert "mock-cloudflare-token" not in text and "Bearer" not in text


def test_strangers_get_404_and_portal_admins_403(mcp):
    assert add(mcp, who="other").status_code == 404
    assert post(mcp, "other", "/apps/acme/notes/mcp/remove", server_id="notes", confirmed="yes").status_code == 404
    assert get(mcp, who="other").status_code == 404
    assert add(mcp, who="admin").status_code == 403 and get(mcp, who="admin", environment="test").status_code == 403
    assert not bindings(mcp)
    assert "/mcp/new" not in mcp.client("admin").get("/apps/acme/notes").text


def test_the_posts_need_csrf(mcp):
    for path in ("mcp", "mcp/remove"):
        assert mcp.client("owner").post(f"/apps/acme/notes/{path}", data={"server_id": "notes"}).status_code == 403
        assert mcp.client("owner").post(f"/apps/acme/notes/{path}", data={"server_id": "notes", "csrf_token": "wrong"}).status_code == 403
    assert not bindings(mcp)


def test_prod_and_test_are_separate(mcp):
    response = add(mcp, environment="prod")
    assert response.status_code == 303 and response.headers["location"] == "/apps/acme/notes?environment=prod"
    [req] = bindings(mcp)
    assert req.final["environment"] == "prod" and req.final["mcp"]["gateway"] == "mcp.example.com"
    assert get(mcp, environment="prod").status_code == 409   # prod's change is open
    assert get(mcp, environment="test").status_code == 200
    assert add(mcp, environment="test").status_code == 303


def test_cloudflare_down_is_a_503_with_nothing_to_pick(mcp):
    real = mock_cloudflare()

    class Down(MockCloudflare):
        def _handle(self, request):
            return httpx.Response(500, json={"success": False, "errors": [{"code": 1, "message": "upstream detail"}]})
    with_cloudflare(mcp, Down(real.account_id, real.portals, real.users))
    for params in ({"environment": "test"}, {"environment": "test", "server_id": "notes"}):
        response = get(mcp, **params)
        assert response.status_code == 503 and "Cloudflare could not answer: try again." in response.text
        assert radios(response.text, "server_id") == set() and "upstream detail" not in response.text
    assert get(mcp).status_code == 200   # step 1 asks Cloudflare nothing
    response = add(mcp)
    assert response.status_code == 503 and "Cloudflare could not answer: try again." in response.text and not bindings(mcp)


def test_a_gateway_cloudflare_doesn_t_have_is_said_at_step_1(mcp):
    with_cloudflare(mcp, mcp_gateways={"test": "mcp-elsewhere.example.com", "prod": "mcp.example.com"})
    response = get(mcp, environment="test")
    assert response.status_code == 400 and "The test MCP gateway (mcp-elsewhere.example.com) is not in Cloudflare." in response.text
    assert step_of(response.text) == "1"


def test_without_an_email_the_steps_say_why(mcp):
    import dataclasses
    store = mcp.store
    person = store.get_person(mcp.people["owner"].id)
    original = store.get_person
    store.get_person = lambda pid: dataclasses.replace(person, email=None) if pid == person.id else original(pid)
    text = get(mcp, environment="test").text
    assert "Your sign-in has no email address" in text and radios(text, "server_id") == set()
    response = add(mcp)
    assert response.status_code == 400 and "has no email address" in response.text and not bindings(mcp)


def test_cloudflare_s_text_is_escaped(mcp):
    doc = json.loads(MOCK.read_text())
    doc["portals"][0]["servers"][0]["description"] = "<script>alert(1)</script>"
    with_cloudflare(mcp, mock_cloudflare(doc=doc))
    text = get(mcp, environment="test").text
    assert "<script>alert(1)</script>" not in text and "&lt;script&gt;alert(1)&lt;/script&gt;" in text


def test_the_process_link_comes_from_settings(mcp):
    with_cloudflare(mcp, mcp_process_url="https://wiki.example.test/mcp")
    link = '<a href="https://wiki.example.test/mcp" rel="noopener noreferrer">'
    assert link in get(mcp, environment="test").text and link in mcp.client("owner").get("/apps/acme/notes").text


def test_mcp_works_without_the_directory(portal):
    portal.app.state.settings = make_settings(cloudflare=mock_cloudflare())
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "MCP servers (Cloudflare gateway)" in page and "Associate Azure Identity" not in page
    add_and_merge(portal)
    assert post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes").status_code == 404
    assert post(portal, "owner", "/apps/acme/notes/mcp/remove", server_id="notes", confirmed="yes").status_code == 303


def test_the_steps_pages_are_free_of_inline_style_and_script(mcp):
    from test_portal_csp_pages import problems
    for params in ({}, {"environment": "test"}, {"environment": "test", "server_id": "notes"}, {"environment": "test", "server_id": "tickets"}):
        assert problems(get(mcp, **params).text) == [], params


# ---- a gateway the platform moved (review fixes, batch 4) ----

def test_after_a_gateway_move_one_confirmed_remove_clears_the_environment(portal):
    from test_portal_apps import with_secret_server
    with_cloudflare(portal, many_servers())
    add_and_merge(portal)
    add_and_merge(portal, "s00")
    with_secret_server(portal, cloudflare=mock_cloudflare(), mcp_gateways={"test": "mcp-new.example.com", "prod": "mcp.example.com"})
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "The platform moved test to a new gateway" in page
    ask = portal.client("owner").get("/apps/acme/notes", params={"confirm_mcp": "notes"}).text
    assert "Remove every MCP server on the old gateway?" in ask and "this removes all 2 servers on mcp-test.example.com" in ask
    from test_portal_apps import add_secret
    refused = add_secret(portal)
    assert refused.status_code == 400 and "remove its MCP servers first" in html.unescape(refused.text) and len(bindings(portal)) == 2
    response = post(portal, "owner", "/apps/acme/notes/mcp/remove", server_id="notes", confirmed="yes")
    assert response.status_code == 303
    assert bindings(portal)[0].final["mcp"] == {"gateway": "mcp-new.example.com", "servers": []}
