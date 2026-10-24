"""A minimal GitHub REST client over httpx. Errors carry the status, method and path only: GitHub's text never reaches a page,
a log line or a request's failure reason. The only headers kept are the two that tell a rate limit from a refusal (GitHub answers
403 for both): x-ratelimit-remaining and retry-after."""

import httpx

KEPT_HEADERS = ("x-ratelimit-remaining", "retry-after")


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
