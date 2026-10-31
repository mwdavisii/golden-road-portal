"""A minimal GitHub REST client over httpx. Errors carry the status, method and path only: GitHub's text never reaches a page,
a log line or a request's failure reason. The only headers kept are the two that tell a rate limit from a refusal (GitHub answers
403 for both): x-ratelimit-remaining and retry-after. `download` follows GitHub's one redirect to its storage without the token."""

import time
from urllib.parse import urlsplit

import httpx

KEPT_HEADERS = ("x-ratelimit-remaining", "retry-after")
DOWNLOAD_DEADLINE = 120  # seconds for one download: a safety cap on a stalled transfer, not a budget (a slow one is retried next pass)


def host_allowed(host: str, patterns) -> bool:
    """`host` is one of `patterns`: an exact name, or *.suffix for any name under it."""
    host = host.lower().rstrip(".")
    return any(host == p or (p.startswith("*.") and host.endswith(p[1:]) and len(host) > len(p) - 1) for p in (q.lower() for q in patterns))


def _kept(response) -> dict:
    return {name: response.headers[name] for name in KEPT_HEADERS if name in response.headers}


class GitHubError(Exception):
    def __init__(self, status: int, method: str, path: str, what: str = "unexpected status", headers: dict | None = None):
        super().__init__(f"{what}: {status} {method} {path}")
        self.status, self.method, self.path, self.headers = status, method, path, dict(headers or {})

    @property
    def rate_limited(self) -> bool:
        """429, or a 403 that carries the signs of a rate limit (no requests remaining, or a retry-after)."""
        return self.status == 429 or (self.status == 403 and (self.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in self.headers))


class HostRefused(GitHubError):
    """A download redirect the client won't follow: not https, with user info, or to a host not allowed. A setting or the egress is
    wrong, not the artifact: the caller tries again once it is fixed."""


class TooLarge(Exception):
    """A download that would be more than its cap. Nothing more is read."""


class GitHub:
    def __init__(self, api_base: str, http: httpx.Client, token):
        self.api_base, self.http, self.token = api_base.rstrip("/"), http, token

    def _headers(self):
        return {"Authorization": f"Bearer {self.token()}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

    def call(self, method, path, *, params=None, json=None, ok=(200,)) -> httpx.Response:
        try:
            response = self.http.request(method, self.api_base + path, params=params, json=json, headers=self._headers(), timeout=30)
        except httpx.HTTPError as exc:
            raise GitHubError(0, method, path, "GitHub can't be reached") from exc
        if response.status_code not in ok:
            raise GitHubError(response.status_code, method, path, headers=_kept(response))
        return response

    def download(self, path, *, cap: int, hosts, deadline: float = DOWNLOAD_DEADLINE) -> bytes:
        """A redirect download, an Actions artifact's zip (spec 2026-10-31 R10). The API answers 302 with a short-lived URL on its storage
        host, which must be one of `hosts` (patterns: name or *.suffix). The token goes to the API only: the storage URL is fetched without
        it, as stored bytes (Accept-Encoding: identity; an encoded answer is refused), and no further redirect is followed. At most `cap`
        bytes are read (more is TooLarge) within `deadline` seconds (past it, a status-0 GitHubError: tried again next pass). The URL
        carries a signature, so it is never put in an error, a log line or the database."""
        try:
            response = self.http.request("GET", self.api_base + path, headers=self._headers(), timeout=30, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise GitHubError(0, "GET", path, "GitHub can't be reached") from exc
        if response.status_code != 302:
            raise GitHubError(response.status_code, "GET", path, headers=_kept(response))
        location = response.headers.get("location", "")
        parts = urlsplit(location)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise HostRefused(302, "GET", path, "the download is not an https URL")
        if not host_allowed(parts.hostname, hosts):
            raise HostRefused(302, "GET", path, "the download is not on an allowed storage host")
        chunks, total, started = [], 0, time.monotonic()
        try:
            with self.http.stream("GET", location, headers={"Accept-Encoding": "identity"}, timeout=60, follow_redirects=False) as streamed:
                if streamed.status_code != 200:
                    raise GitHubError(streamed.status_code, "GET", path, "the download failed")
                if streamed.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                    raise GitHubError(200, "GET", path, "the download is encoded")
                for chunk in streamed.iter_raw():
                    total += len(chunk)
                    if total > cap:
                        raise TooLarge(path)
                    if time.monotonic() - started > deadline:
                        raise GitHubError(0, "GET", path, "the download took too long")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise GitHubError(0, "GET", path, "the download can't be reached") from exc
        return b"".join(chunks)

    def paginate(self, path, *, key=None, params=None, max_pages=50):
        url, query = self.api_base + path, {**(params or {}), "per_page": 100}
        for _ in range(max_pages):
            try:
                response = self.http.get(url, params=query, headers=self._headers(), timeout=30)
            except httpx.HTTPError as exc:
                raise GitHubError(0, "GET", path, "GitHub can't be reached") from exc
            if response.status_code != 200:
                raise GitHubError(response.status_code, "GET", path, headers=_kept(response))
            data = response.json()
            yield from (data[key] if key else data)
            following = response.links.get("next", {}).get("url")
            if not following:
                return
            if not following.startswith(self.api_base + "/"):
                raise GitHubError(200, "GET", path, "a next page leaves the API host")
            url, query = following, None
        raise GitHubError(200, "GET", path, "too many pages")
