"""The Cloudflare client (spec 2026-10-31 Part 1): one request in the real API's shape, the account's MCP server portals, every page
read with no page cap, the gateway found by its exact hostname, and errors that are clear, never a gateway, and never Cloudflare's
own text."""

import httpx
import pytest

from portal.cloudflare import API, Client, CloudflareError

ACCOUNT = "0123456789abcdef0123456789abcdef"
PATH = f"/client/v4/accounts/{ACCOUNT}/access/ai-controls/mcp/portals"


def server(sid, **over):
    return {"server_id": sid, "name": sid.title(), "description": "", "hostname": f"{sid}.internal.example", "auth_type": "unauthenticated",
            "on_behalf": False, "status": "ready", "tools": [{"name": "search"}], **over}


def portal(host, servers):
    return {"id": host.split(".")[0], "hostname": host, "name": host, "servers": servers}


def client(pages, seen):
    def handle(request):
        seen.append((request.method, request.url.path, dict(request.url.params), request.headers.get("authorization")))
        if (request.method, request.url.path) != ("GET", PATH):
            return httpx.Response(404, json={"success": False, "errors": [{"code": 7003, "message": "No route"}]})
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"success": True, "errors": [], "messages": [], "result": pages[page - 1],
                                         "result_info": {"page": page, "per_page": 100, "count": len(pages[page - 1]),
                                                         "total_count": sum(map(len, pages)), "total_pages": len(pages)}})
    return Client(ACCOUNT, "api-token", http=httpx.Client(transport=httpx.MockTransport(handle)))


def test_the_gateway_is_found_by_its_exact_hostname_with_its_servers():
    seen = []
    c = client([[portal("mcp-test.example.com.evil", []), portal("mcp-test.example.com", [server("notes"), server("tickets", on_behalf=True)])]], seen)
    gw = c.gateway("mcp-test.example.com")
    assert gw.hostname == "mcp-test.example.com" and [s.server_id for s in gw.servers] == ["notes", "tickets"]
    assert gw.servers[1].on_behalf is True and gw.servers[0].tools == ("search",)
    assert seen == [("GET", PATH, {"search": "mcp-test.example.com", "page": "1", "per_page": "100"}, "Bearer api-token")]
    assert API == "https://api.cloudflare.com/client/v4"


def test_the_hostname_is_matched_without_regard_to_case():
    gw = client([[portal("MCP-Test.example.com", [server("notes")])]], []).gateway("mcp-test.example.com")
    assert gw is not None and gw.id == "MCP-Test" and gw.servers[0].name == "Notes"


def test_every_page_is_read():
    seen = []
    c = client([[portal("a.example.com", [])], [portal("mcp-test.example.com", [server("notes")])]], seen)
    assert c.gateway("mcp-test.example.com").servers[0].server_id == "notes" and [p["page"] for _, _, p, _ in seen] == ["1", "2"]


def test_there_is_no_page_cap():
    pages = [[portal(f"p{n}.example.com", [])] for n in range(60)] + [[portal("mcp-test.example.com", [])]]
    seen = []
    assert client(pages, seen).gateway("mcp-test.example.com").servers == () and len(seen) == 61


def test_an_empty_page_ends_the_walk():
    seen = []

    def handle(request):
        seen.append(request.url.params["page"])
        return httpx.Response(200, json={"success": True, "errors": [], "messages": [], "result": [],
                                         "result_info": {"page": 1, "per_page": 100, "count": 0, "total_count": 5, "total_pages": 9}})
    c = Client(ACCOUNT, "t", http=httpx.Client(transport=httpx.MockTransport(handle)))
    assert c.gateway("mcp-test.example.com") is None and seen == ["1"]


def test_no_such_gateway_is_none():
    assert client([[]], []).gateway("mcp-test.example.com") is None


@pytest.mark.parametrize("answer", [httpx.Response(403, json={"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}),
                                    httpx.Response(200, json={"success": False, "errors": [{"code": 1, "message": "x"}]}),
                                    httpx.Response(200, text="not json"), httpx.Response(200, json={"success": True, "result": "x"}),
                                    httpx.Response(200, json=["Authentication error"]), httpx.Response(500),
                                    httpx.Response(200, json={"success": True, "result": ["Authentication error"]})])
def test_errors_are_clear_and_never_a_gateway(answer):
    c = Client(ACCOUNT, "t", http=httpx.Client(transport=httpx.MockTransport(lambda r: answer)))
    with pytest.raises(CloudflareError) as exc:
        c.gateway("mcp-test.example.com")
    assert "Authentication error" not in str(exc.value)  # Cloudflare's text never reaches a page


def test_a_redirect_is_never_followed():
    seen = []

    def handle(request):
        seen.append(request.url.path)
        return httpx.Response(302, headers={"location": "https://elsewhere.example/x"})
    c = Client(ACCOUNT, "t", http=httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(CloudflareError, match="redirect"):
        c.gateway("mcp-test.example.com")
    assert seen == [PATH]


def test_an_unreachable_cloudflare_names_no_token():
    def handle(request):
        raise httpx.ConnectError(f"failed with {request.headers['authorization']}", request=request)
    c = Client(ACCOUNT, "secret-api-token", http=httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(CloudflareError, match="could not be reached: ConnectError") as exc:
        c.gateway("mcp-test.example.com")
    assert "secret-api-token" not in str(exc.value) and exc.value.__context__ is None and exc.value.__cause__ is None


@pytest.mark.parametrize("field", ["server_id", "name", "description", "hostname", "auth_type", "on_behalf", "status", "tools"])
def test_a_server_missing_a_field_is_an_error_not_a_guess(field):
    bad = {k: v for k, v in server("notes").items() if k != field}
    with pytest.raises(CloudflareError, match=field):
        client([[portal("mcp-test.example.com", [bad])]], []).gateway("mcp-test.example.com")


@pytest.mark.parametrize("field, value", [("on_behalf", "false"), ("on_behalf", 0), ("tools", "search"), ("tools", [{"title": "x"}]),
                                          ("tools", ["search"]), ("server_id", ""), ("server_id", 7), ("status", None)])
def test_a_server_field_of_the_wrong_type_is_an_error(field, value):
    with pytest.raises(CloudflareError, match=field):
        client([[portal("mcp-test.example.com", [server("notes", **{field: value})])]], []).gateway("mcp-test.example.com")


@pytest.mark.parametrize("edit", [lambda p: p.pop("servers"), lambda p: p.update(servers="notes"), lambda p: p.update(id=None),
                                  lambda p: p.update(name=5), lambda p: p.update(servers=["notes"]),
                                  lambda p: p.update(servers=[server("notes"), server("notes")])])
def test_a_bad_portal_is_an_error(edit):
    p = portal("mcp-test.example.com", [server("notes")])
    edit(p)
    with pytest.raises(CloudflareError):
        client([[p]], []).gateway("mcp-test.example.com")


def test_the_client_closes_its_http_client():
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with Client(ACCOUNT, "t", http=http):
        pass
    assert http.is_closed


def answer(page, portals, total_pages):
    return httpx.Response(200, json={"success": True, "errors": [], "messages": [], "result": portals,
                                     "result_info": {"page": page, "per_page": 100, "count": len(portals), "total_count": 9,
                                                     "total_pages": total_pages}})


def test_an_answer_for_another_page_is_an_error():
    c = Client(ACCOUNT, "t", http=httpx.Client(transport=httpx.MockTransport(lambda r: answer(1, [portal("a.example.com", [])], 3))))
    with pytest.raises(CloudflareError, match="page"):
        c.gateway("mcp-test.example.com")  # page 2 is answered as page 1


def test_a_page_repeating_the_last_ones_portals_is_an_error():
    def handle(request):
        page = int(request.url.params["page"])
        return answer(page, [portal("a.example.com", []), portal("b.example.com", [])], 5)
    c = Client(ACCOUNT, "t", http=httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(CloudflareError, match="repeats"):
        c.gateway("mcp-test.example.com")


@pytest.mark.parametrize("account, token", [("0123456789abcdef0123456789abcde/", "api-token-1"), ("../accounts/x", "api-token-1"),
                                            (ACCOUNT.upper(), "api-token-1"), (ACCOUNT, "tök-token"), (ACCOUNT, "token\nX-Other: 1"),
                                            (ACCOUNT, ""), (7, "api-token-1"), (ACCOUNT, None)])
def test_a_bad_account_id_or_token_is_refused_before_any_request(account, token):
    seen = []
    with pytest.raises(CloudflareError, match="not valid") as exc:
        Client(account, token, http=httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(404))))
    assert seen == [] and (not isinstance(token, str) or not token or token not in str(exc.value))


@pytest.mark.parametrize("failure", [httpx.InvalidURL("bad"), UnicodeEncodeError("ascii", "x", 0, 1, "no")])
def test_a_request_that_cannot_be_built_is_an_error(failure):
    def handle(request):
        raise failure
    c = Client(ACCOUNT, "t", http=httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(CloudflareError, match="could not be reached"):
        c.gateway("mcp-test.example.com")
