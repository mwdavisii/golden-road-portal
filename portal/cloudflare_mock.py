"""The mock Cloudflare (spec 2026-10-31 Part 1, "The mock"): an httpx.MockTransport that answers exactly the request in the client's
table (portal.cloudflare), in Cloudflare's API v4 response shapes, over the dummy data in mock_cloudflare.json.

- Two MCP server portals, one per environment. Each portal's `environment` is the mock's own key (which configured gateway host
  it takes, load_mock(gateways=...)); answers leave it out.
- One API token, TOKEN, is accepted; any other or none is 403 in Cloudflare's error shape.
- Anything outside the table answers 404: another method on the portals path, another path, another account, another host.
- The per-user policy (`users`, servers_for) is mock only (spec M8): it stands in for Cloudflare Access evaluating the person
  against each server's Access policies, which the client never asks for. A person is their identity provider email."""

import dataclasses as dc
import json
import math
import re
import unicodedata
from pathlib import Path

import httpx

from .cloudflare import Client

MOCK = Path(__file__).resolve().parent / "mock_cloudflare.json"
HOST = "api.cloudflare.mock"
BASE_URL = f"https://{HOST}/client/v4"
PREFIX = "/client/v4"
TOKEN = "mock-cloudflare-token"
ENVIRONMENTS = ("test", "prod")
ACCOUNT_ID = re.compile(r"[0-9a-f]{32}")
# builder.config's GATEWAY: the host deploy-verify holds bindings.yaml's mcp.gateway to
HOSTNAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")
MAX_HOSTNAME = 253
SERVER_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")  # binding/v2's mcp.servers[].server_id
PORTAL_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
WORD = re.compile(r"[a-z][a-z_]{0,31}")  # auth_type and status
TOOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s.]+(\.[^@\s.]+)+")
MAX_NAME = 200
MAX_DESCRIPTION = 500
PAGE = re.compile(r"[0-9]{1,6}")  # ASCII digits only: str.isdigit takes other scripts' digits and superscripts
DEFAULT_PER_PAGE = 20  # confirm against Cloudflare's API reference: the list's default and largest per_page
MAX_PER_PAGE = 100  # Cloudflare's documented maximum
PORTAL_KEYS = {"id", "hostname", "name", "environment", "servers"}
SERVER_KEYS = {"server_id", "name", "description", "hostname", "auth_type", "on_behalf", "status", "tools"}


class MockCloudflareError(ValueError):
    pass


@dc.dataclass(frozen=True)
class Portal:
    id: str
    hostname: str
    name: str
    environment: str
    servers: tuple  # servers[] records in Cloudflare's shape, checked

    def answer(self) -> dict:
        return {"id": self.id, "hostname": self.hostname, "name": self.name, "servers": json.loads(json.dumps(list(self.servers)))}


@dc.dataclass(frozen=True)
class User:
    email: str  # lowercase
    servers: frozenset


def _keys(entry, keys, what):
    if not isinstance(entry, dict) or set(entry) != keys:
        raise MockCloudflareError(f"the mock Cloudflare's {what} has missing or unknown keys")


def _list(value, what):
    if not isinstance(value, list):
        raise MockCloudflareError(f"the mock Cloudflare's {what} must be a list")
    return value


def _text(value, pattern, what):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise MockCloudflareError(f"the mock Cloudflare's {what} is not valid")
    return value


def _host(value, what):
    if not isinstance(value, str) or len(value) > MAX_HOSTNAME or not HOSTNAME.fullmatch(value):
        raise MockCloudflareError(f"the mock Cloudflare's {what} is not a lowercase host name")
    return value


def _words(value, what, low, high):
    if not isinstance(value, str) or not low <= len(value) <= high or any(unicodedata.category(c) in ("Cc", "Cf") for c in value):
        raise MockCloudflareError(f"the mock Cloudflare's {what} must be {low} to {high} characters with no control or format "
                                  "characters")
    return value


def _unique(values, what):
    values = list(values)
    if len(set(values)) != len(values):
        raise MockCloudflareError(f"the mock Cloudflare lists a {what} twice")


def _gateways(gateways) -> dict:
    """{environment: host} from the settings' PORTAL_MCP_GATEWAY_*, checked as the platform config's mcp_gateways is."""
    if gateways is None:
        return {}
    if not isinstance(gateways, dict) or not set(gateways) <= set(ENVIRONMENTS):
        raise MockCloudflareError(f"the mock Cloudflare's gateways must map {' and '.join(ENVIRONMENTS)} to host names")
    out = {env: _host(host, f"{env} gateway") for env, host in gateways.items()}
    _unique(out.values(), "gateway host")
    return out


def _server(s) -> dict:
    _keys(s, SERVER_KEYS, "server")
    _text(s["server_id"], SERVER_ID, "server id")
    _words(s["name"], "server name", 1, MAX_NAME)
    _words(s["description"], "server description", 0, MAX_DESCRIPTION)
    _host(s["hostname"], "server hostname")
    _text(s["auth_type"], WORD, "server auth_type")
    _text(s["status"], WORD, "server status")
    if type(s["on_behalf"]) is not bool:
        raise MockCloudflareError("the mock Cloudflare's on_behalf must be true or false")
    for tool in _list(s["tools"], "server tools"):
        _keys(tool, {"name"}, "tool")
        _text(tool["name"], TOOL, "tool name")
    _unique((t["name"] for t in s["tools"]), "tool in one server")
    return json.loads(json.dumps(s))


def parse(doc, gateways=None) -> "MockCloudflare":
    """The mock from its document. `gateways` ({"test": host, "prod": host}) replaces each portal's hostname by its environment."""
    hosts = _gateways(gateways)
    _keys(doc, {"schema", "account_id", "portals", "users"}, "document")
    if doc["schema"] != "mock-cloudflare/v1":
        raise MockCloudflareError("the mock Cloudflare is not a mock-cloudflare/v1")
    account = _text(doc["account_id"], ACCOUNT_ID, "account_id")
    portals = []
    for p in _list(doc["portals"], "portals"):
        _keys(p, PORTAL_KEYS, "portal")
        if p["environment"] not in ENVIRONMENTS:
            raise MockCloudflareError(f"the mock Cloudflare's portal environment must be one of {', '.join(ENVIRONMENTS)}")
        _host(p["hostname"], "portal hostname")
        servers = tuple(_server(s) for s in _list(p["servers"], "portal servers"))
        _unique((s["server_id"] for s in servers), "server id in one portal")
        portals.append(Portal(_text(p["id"], PORTAL_ID, "portal id"), hosts.get(p["environment"], p["hostname"]),
                              _words(p["name"], "portal name", 1, MAX_NAME), p["environment"], servers))
    _unique((p.id for p in portals), "portal id")
    _unique((p.environment for p in portals), "portal environment")
    _unique((p.hostname.lower() for p in portals), "portal hostname")
    known = {s["server_id"] for p in portals for s in p.servers}
    users = []
    for u in _list(doc["users"], "users"):
        _keys(u, {"email", "servers"}, "user")
        email = _text(u["email"], EMAIL, "user's email").lower()
        servers = _list(u["servers"], "user's servers")
        if not all(isinstance(s, str) for s in servers) or not set(servers) <= known:
            raise MockCloudflareError("a mock Cloudflare user names a server the mock does not list")
        users.append(User(email, frozenset(servers)))
    _unique((u.email for u in users), "user email")
    return MockCloudflare(account, tuple(portals), tuple(users))


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise MockCloudflareError("the mock Cloudflare names a key twice in one object")
    return dict(pairs)


def load_mock(gateways=None) -> "MockCloudflare":
    try:
        return parse(json.loads(MOCK.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys), gateways)
    except MockCloudflareError:
        raise
    except (OSError, ValueError, RecursionError) as exc:
        raise MockCloudflareError(f"the mock Cloudflare can't be loaded: {type(exc).__name__}") from None


def _error(status, code, message):
    return httpx.Response(status, json={"success": False, "errors": [{"code": code, "message": message}], "messages": [], "result": None})


@dc.dataclass(frozen=True)
class MockCloudflare:
    name = "mock-cloudflare"  # what binding/v2 records as an MCP server's verified_by
    account_id: str
    portals: tuple[Portal, ...]
    users: tuple[User, ...]

    @property
    def gateways(self) -> dict:
        """{environment: the gateway host the mock serves for it}."""
        return {p.environment: p.hostname for p in self.portals}

    def servers_for(self, email) -> frozenset:
        """Mock only (spec M8): the server ids the person with this identity provider email may use, on any gateway. The real
        version evaluates the person against each server's Cloudflare Access policies; no email is no servers."""
        if not isinstance(email, str) or not email:
            return frozenset()
        return next((u.servers for u in self.users if u.email == email.lower()), frozenset())

    def client(self) -> Client:
        return Client(self.account_id, TOKEN, http=httpx.Client(transport=self.transport()), base=BASE_URL)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if (request.url.host != HOST or request.method != "GET"
                or path != f"{PREFIX}/accounts/{self.account_id}/access/ai-controls/mcp/portals"):
            return _error(404, 7003, "No route for that URI")  # confirm: Cloudflare's code for an unknown route
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return _error(403, 10000, "Authentication error")
        return self._portals(request)

    def _portals(self, request):
        raw_page, raw_per_page = request.url.params.get("page", "1"), request.url.params.get("per_page", str(DEFAULT_PER_PAGE))
        if not PAGE.fullmatch(raw_page) or not PAGE.fullmatch(raw_per_page):
            return _error(400, 1001, "page and per_page must be whole numbers")
        page, per_page = int(raw_page), int(raw_per_page)
        if page < 1 or not 1 <= per_page <= MAX_PER_PAGE:
            return _error(400, 1001, f"page must be 1 or more and per_page from 1 to {MAX_PER_PAGE}")
        search = request.url.params.get("search", "").lower()
        found = [p.answer() for p in self.portals if search in p.hostname.lower()]
        result = found[(page - 1) * per_page:page * per_page]
        return httpx.Response(200, json={"success": True, "errors": [], "messages": [], "result": result,
                                         "result_info": {"page": page, "per_page": per_page, "count": len(result),
                                                         "total_count": len(found), "total_pages": math.ceil(len(found) / per_page)}})
