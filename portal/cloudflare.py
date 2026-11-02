"""A Cloudflare client for the app page's MCP servers (spec 2026-10-31 Part 1), written against Cloudflare's API v4: one request, the
account's MCP server portals with their servers, read with an API token that has MCP Portals Read. It never writes: the token and
the servers' Service Auth policies are a platform job's, from cloudflare-access.yaml. In the mock its transport is
portal.cloudflare_mock; against a real account, confirm the marked details in Cloudflare's API reference. No error message carries
the token or Cloudflare's own text."""

import dataclasses as dc
import re

import httpx

API = "https://api.cloudflare.com/client/v4"
TIMEOUT = 10.0  # seconds, for connect, read, write and pool alike
PER_PAGE = 100
ACCOUNT_ID = re.compile(r"[0-9a-f]{32}")
TOKEN = re.compile(r"[\x21-\x7e]+")  # printable ASCII, no space: it goes into a header as it is
SERVER_FIELDS = ("server_id", "name", "description", "hostname", "auth_type", "on_behalf", "status", "tools")


class CloudflareError(Exception):
    pass


@dc.dataclass(frozen=True)
class Server:
    server_id: str
    name: str
    description: str
    hostname: str
    auth_type: str
    on_behalf: bool  # the server acts as the signed-in person (OAuth on their behalf), which a service token can't do
    status: str  # "ready" is the only status a picker offers
    tools: tuple  # the tools' names


@dc.dataclass(frozen=True)
class Gateway:
    id: str
    hostname: str
    name: str
    servers: tuple  # Server, in Cloudflare's order


def _server(s, hostname) -> Server:
    """One servers[] record, every field present with its type, else an error naming the field (never a guess at a default).
    confirm against Cloudflare's API reference: the servers[] field names and on_behalf."""
    if not isinstance(s, dict):
        raise CloudflareError(f"a server on {hostname} is not an object")
    for field in SERVER_FIELDS:
        value = s.get(field)
        if field == "on_behalf":
            ok = type(value) is bool
        elif field == "tools":
            ok = isinstance(value, list) and all(isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"] for t in value)
        elif field in ("server_id", "name", "hostname", "auth_type", "status"):
            ok = isinstance(value, str) and bool(value)
        else:  # description may be empty
            ok = isinstance(value, str)
        if not ok:
            raise CloudflareError(f"a server on {hostname} has no valid {field}")
    return Server(s["server_id"], s["name"], s["description"], s["hostname"], s["auth_type"], s["on_behalf"], s["status"],
                  tuple(t["name"] for t in s["tools"]))


def _gateway(p, hostname) -> Gateway:
    for field in ("id", "name"):
        if not isinstance(p.get(field), str) or not p[field]:
            raise CloudflareError(f"the MCP portal {hostname} has no valid {field}")
    if not isinstance(p.get("servers"), list):
        raise CloudflareError(f"the MCP portal {hostname} has no servers list")
    servers = tuple(_server(s, hostname) for s in p["servers"])
    if len({s.server_id for s in servers}) != len(servers):
        raise CloudflareError(f"the MCP portal {hostname} lists a server id twice")
    return Gateway(p["id"], p["hostname"], p["name"], servers)


class Client:
    def __init__(self, account_id: str, api_token: str, *, http: httpx.Client, base: str = API):
        if not isinstance(account_id, str) or not ACCOUNT_ID.fullmatch(account_id):
            raise CloudflareError("the Cloudflare account id is not valid (32 lowercase hex characters)")
        if not isinstance(api_token, str) or not TOKEN.fullmatch(api_token):
            raise CloudflareError("the Cloudflare API token is not valid (printable ASCII)")
        self.path = f"{base.rstrip('/')}/accounts/{account_id}/access/ai-controls/mcp/portals"
        self.token, self.http = api_token, http

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        """Close the httpx client this one sends through."""
        close = getattr(self.http, "close", None)
        if callable(close):
            close()

    def _page(self, hostname, page):
        """(the portals on one page, result_info.total_pages). Never redirected. confirm against Cloudflare's API reference: search,
        page, per_page and result_info.total_pages on the MCP portals list."""
        failure = None
        try:
            r = self.http.get(self.path, params={"search": hostname, "page": page, "per_page": PER_PAGE},
                              headers={"Authorization": f"Bearer {self.token}"}, timeout=TIMEOUT, follow_redirects=False)
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as exc:  # never the exception's text, nor the exception itself: it may carry the request
            failure = type(exc).__name__
        if failure is not None:  # raised outside the except block, so the httpx exception is not its __context__
            raise CloudflareError(f"Cloudflare could not be reached: {failure}")
        if r.is_redirect or 300 <= r.status_code < 400:
            raise CloudflareError(f"Cloudflare answered a redirect ({r.status_code}) for the MCP portals: redirects are never followed")
        try:
            doc = r.json()
        except ValueError:
            raise CloudflareError(f"Cloudflare's answer ({r.status_code}) for the MCP portals is not JSON") from None
        if r.status_code != 200 or not isinstance(doc, dict) or doc.get("success") is not True or not isinstance(doc.get("result"), list):
            raise CloudflareError(f"Cloudflare answered {r.status_code} for the MCP portals")  # never Cloudflare's own text
        if not all(isinstance(p, dict) for p in doc["result"]):
            raise CloudflareError("Cloudflare's answer for the MCP portals has a portal that is not an object")
        info = doc.get("result_info")
        if not isinstance(info, dict) or type(info.get("page")) is not int or info["page"] != page:
            raise CloudflareError(f"Cloudflare's answer for page {page} of the MCP portals is for another page")
        return doc["result"], info.get("total_pages")

    def gateway(self, hostname: str) -> Gateway | None:
        """The portal whose hostname is exactly `hostname` (case-insensitive), with its servers, or None. Every page is read: there is
        no page cap; the answer's total_pages ends the walk, and an empty page ends it too."""
        page, last = 1, None
        while True:
            result, total = self._page(hostname, page)
            ids = [p.get("id") for p in result]
            if result and ids == last:  # a server that ignores page would otherwise be walked for ever
                raise CloudflareError(f"Cloudflare's page {page} of the MCP portals repeats page {page - 1}")
            last = ids
            for p in result:
                if isinstance(p.get("hostname"), str) and p["hostname"].lower() == hostname.lower():
                    return _gateway(p, hostname)
            if not result or type(total) is not int or page >= total:
                return None
            page += 1
