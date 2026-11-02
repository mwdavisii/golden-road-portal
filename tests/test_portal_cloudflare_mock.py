"""The mock Cloudflare (spec 2026-10-31 Part 1, "The mock"): the MCP server portals in Cloudflare's response shapes over dummy data,
the configured gateway hosts, 404 for anything outside the client's table, 403 without the mock's token, a strict loader, and the
mock-only per-user policy (M8)."""

import copy
import json

import httpx
import pytest

from portal.cloudflare_mock import MOCK, TOKEN, MockCloudflareError, load_mock, parse

SHIPPED = json.loads(MOCK.read_text(encoding="utf-8"))
BASE = "https://api.cloudflare.mock/client/v4"


@pytest.fixture
def mock():
    return load_mock()


@pytest.fixture
def raw(mock):
    """A plain httpx client on the mock's transport, to see exactly what it answers."""
    return httpx.Client(transport=mock.transport(), base_url=BASE)


def portals_path(mock):
    return f"/accounts/{mock.account_id}/access/ai-controls/mcp/portals"


def auth(token=TOKEN):
    return {"authorization": f"Bearer {token}"}


def test_the_shipped_mock_serves_both_gateways(mock):
    assert mock.name == "mock-cloudflare" and mock.gateways == {"test": "mcp-test.example.com", "prod": "mcp.example.com"}
    test = mock.client().gateway("mcp-test.example.com")
    prod = mock.client().gateway("mcp.example.com")
    assert [s.server_id for s in test.servers] == ["notes", "tickets", "docs-search"]
    assert [s.server_id for s in prod.servers] == ["notes", "tickets", "finance-ledger"]
    by_id = {s.server_id: s for s in test.servers}
    assert by_id["docs-search"].status == "error" and by_id["tickets"].on_behalf is True and by_id["notes"].on_behalf is False
    assert by_id["notes"].tools == ("search_notes", "create_note") and by_id["docs-search"].tools == ()


def test_the_configured_gateway_hosts_replace_the_placeholders():
    m = load_mock(gateways={"test": "mcp-test.acme.example", "prod": "mcp.acme.example"})
    assert m.gateways == {"test": "mcp-test.acme.example", "prod": "mcp.acme.example"}
    assert [s.server_id for s in m.client().gateway("mcp-test.acme.example").servers] == ["notes", "tickets", "docs-search"]
    assert "finance-ledger" in {s.server_id for s in m.client().gateway("mcp.acme.example").servers}
    assert m.client().gateway("mcp-test.example.com") is None and m.client().gateway("mcp.example.com") is None


@pytest.mark.parametrize("gateways", [{"test": "mcp.example.com", "prod": "mcp.example.com"}, {"test": "not a host", "prod": "mcp.x.example"},
                                      {"stage": "mcp-stage.example.com"}, {"test": 7}, ["mcp.example.com"]])
def test_bad_gateway_hosts_are_refused(gateways):
    with pytest.raises(MockCloudflareError):
        load_mock(gateways=gateways)


def test_the_per_user_policy_is_by_email_without_regard_to_case(mock):
    assert mock.servers_for("mwdavisii@example.com") == {"notes", "tickets", "docs-search"}
    assert mock.servers_for("MWDavisII@Example.com") == {"notes", "tickets", "docs-search"}
    assert mock.servers_for("other@example.com") == {"finance-ledger"}
    assert mock.servers_for("nobody@example.com") == set() and mock.servers_for(None) == set() and mock.servers_for("") == set()
    assert isinstance(mock.servers_for("other@example.com"), frozenset)


def test_answers_are_in_cloudflares_shape_without_the_mocks_own_keys(mock, raw):
    r = raw.get(portals_path(mock), params={"search": "mcp-test", "page": 1, "per_page": 100}, headers=auth())
    doc = r.json()
    assert r.status_code == 200 and doc["success"] is True and doc["errors"] == [] and doc["messages"] == []
    assert doc["result_info"] == {"page": 1, "per_page": 100, "count": 1, "total_count": 1, "total_pages": 1}
    [p] = doc["result"]
    assert set(p) == {"id", "hostname", "name", "servers"} and p["hostname"] == "mcp-test.example.com"
    assert set(p["servers"][0]) == {"server_id", "name", "description", "hostname", "auth_type", "on_behalf", "status", "tools"}


def test_search_is_a_case_insensitive_substring_of_the_hostname(mock, raw):
    names = lambda search: [p["hostname"] for p in raw.get(portals_path(mock), params={"search": search}, headers=auth()).json()["result"]]  # noqa: E731
    assert names("EXAMPLE.COM") == ["mcp-test.example.com", "mcp.example.com"] and names("mcp.example") == ["mcp.example.com"]
    assert names("") == ["mcp-test.example.com", "mcp.example.com"] and names("nothing") == []


def test_the_mock_pages_with_page_and_per_page(mock, raw):
    first = raw.get(portals_path(mock), params={"page": 1, "per_page": 1}, headers=auth()).json()
    second = raw.get(portals_path(mock), params={"page": 2, "per_page": 1}, headers=auth()).json()
    past = raw.get(portals_path(mock), params={"page": 3, "per_page": 1}, headers=auth()).json()
    assert [p["id"] for p in first["result"] + second["result"]] == ["mcp-test", "mcp-prod"] and past["result"] == []
    assert first["result_info"] == {"page": 1, "per_page": 1, "count": 1, "total_count": 2, "total_pages": 2}


@pytest.mark.parametrize("params", [{"page": "0"}, {"page": "x"}, {"per_page": "0"}, {"per_page": "101"}, {"page": "¹"}])
def test_bad_paging_is_a_400_in_cloudflares_shape(mock, raw, params):
    r = raw.get(portals_path(mock), params=params, headers=auth())
    assert r.status_code == 400 and r.json()["success"] is False and r.json()["errors"][0]["code"]


@pytest.mark.parametrize("method, path", [("GET", "/accounts/{a}/access/apps"), ("POST", "/accounts/{a}/access/ai-controls/mcp/portals"),
                                          ("PUT", "/accounts/{a}/access/ai-controls/mcp/portals"),
                                          ("GET", "/accounts/{a}/access/ai-controls/mcp/portals/mcp-test"),
                                          ("GET", "/accounts/ffffffffffffffffffffffffffffffff/access/ai-controls/mcp/portals"),
                                          ("GET", "/accounts/{a}/access/ai-controls/mcp/servers"), ("GET", "/user/tokens/verify")])
def test_anything_outside_the_clients_table_is_404(mock, raw, method, path):
    r = raw.request(method, path.format(a=mock.account_id), headers=auth())
    assert r.status_code == 404 and r.json()["success"] is False


def test_another_host_is_404(mock):
    c = httpx.Client(transport=mock.transport())
    assert c.get(f"https://api.cloudflare.com/client/v4{portals_path(mock)}", headers=auth()).status_code == 404


@pytest.mark.parametrize("headers", [{}, auth("wrong"), {"authorization": TOKEN}, {"authorization": f"Basic {TOKEN}"}])
def test_a_missing_or_wrong_token_is_403(mock, raw, headers):
    r = raw.get(portals_path(mock), headers=headers)
    assert r.status_code == 403 and r.json() == {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}],
                                                 "messages": [], "result": None}


def doc():
    return copy.deepcopy(SHIPPED)


def test_the_fixture_parses_through_parse():
    assert parse(doc()).gateways == {"test": "mcp-test.example.com", "prod": "mcp.example.com"}


@pytest.mark.parametrize("edit", [
    lambda d: d.update(extra=1), lambda d: d.update(schema="mock-cloudflare/v2"), lambda d: d.update(account_id="nope"),
    lambda d: d["portals"][0].update(extra=1), lambda d: d["portals"][0]["servers"][0].update(extra=1),
    lambda d: d["portals"][0]["servers"][0].pop("status"), lambda d: d["users"][0].update(extra=1),
    lambda d: d["portals"][0]["servers"].append(copy.deepcopy(d["portals"][0]["servers"][0])),  # a server id twice in one portal
    lambda d: d["users"][0]["servers"].append("unknown"), lambda d: d["portals"][0].update(hostname="Not A Host"),
    lambda d: d["portals"][0]["servers"][0].update(hostname="bad_host"), lambda d: d["portals"][0]["servers"][0].update(server_id="Notes"),
    lambda d: d["portals"][0]["servers"][0].update(server_id="-notes"), lambda d: d["portals"][0]["servers"][0].update(server_id="n" * 65),
    lambda d: d["portals"][0]["servers"][0].update(name="No\x07tes"), lambda d: d["portals"][0].update(name="bad‮name"),
    lambda d: d["portals"][1].update(hostname="MCP-TEST.example.com"),  # two portals with one hostname
    lambda d: d["portals"][1].update(environment="test"), lambda d: d["portals"][0].update(environment="stage"),
    lambda d: d["portals"][0]["servers"][0].update(on_behalf="false"), lambda d: d["portals"][0]["servers"][0].update(tools=["x"]),
    lambda d: d["portals"][0]["servers"][0].update(tools=[{"name": "x", "extra": 1}]), lambda d: d["portals"].append(d["portals"][0]),
    lambda d: d["users"].append({"email": "MWDAVISII@example.com", "servers": []}), lambda d: d["users"][0].update(email="not-an-email"),
    lambda d: d["portals"][0].update(id="mcp-prod"), lambda d: d.update(portals={}), lambda d: d["users"][0].update(servers="notes"),
])
def test_a_bad_file_is_refused(edit):
    d = doc()
    edit(d)
    with pytest.raises(MockCloudflareError):
        parse(d)


def test_a_file_that_does_not_load_is_refused(tmp_path, monkeypatch):
    from portal import cloudflare_mock
    (tmp_path / "m.json").write_text('{"schema": "mock-cloudflare/v1", "schema": "mock-cloudflare/v1"}')
    monkeypatch.setattr(cloudflare_mock, "MOCK", tmp_path / "m.json")
    with pytest.raises(MockCloudflareError, match="twice"):
        load_mock()
    monkeypatch.setattr(cloudflare_mock, "MOCK", tmp_path / "missing.json")
    with pytest.raises(MockCloudflareError, match="can't be loaded"):
        load_mock()


def test_per_page_is_at_most_100_as_cloudflare_documents(mock, raw):
    assert raw.get(portals_path(mock), params={"per_page": "100"}, headers=auth()).status_code == 200


def test_deeply_nested_json_is_refused(tmp_path, monkeypatch):
    from portal import cloudflare_mock
    (tmp_path / "m.json").write_text("[" * 100000 + "]" * 100000)
    monkeypatch.setattr(cloudflare_mock, "MOCK", tmp_path / "m.json")
    with pytest.raises(MockCloudflareError, match="can't be loaded"):
        load_mock()
