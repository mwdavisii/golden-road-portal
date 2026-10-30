"""A Delinea Secret Server client for the app page (spec 2026-10-29 Delinea secrets), written against Secret Server's REST API:
POST <base>/oauth2/token, then Bearer tokens on <base>/api/v1. It reads only what a picker needs (search, folders, a template's
fields, and whether the platform's account can see a secret) and never a value: no /fields/ request, no GET /api/v1/secrets/{id}.
In the mock its transport is portal.secretserver_mock; against a real instance, confirm the three marked details in RestApiDocs
(<base>/RestApiDocs.ashx?doc=OpenAPI). No error message carries a token, a username or a password."""

import dataclasses as dc

import httpx

TIMEOUT = 10.0  # seconds, for connect, read, write and pool alike


class SecretServerError(Exception):
    pass


@dc.dataclass(frozen=True)
class SecretSummary:
    id: int
    name: str
    folder_id: int
    template_id: int
    template_name: str


@dc.dataclass(frozen=True)
class Field:
    slug: str
    display_name: str
    is_password: bool


def _status_problem(status: int, what: str) -> str:
    if status == 401:
        return f"Secret Server refused the token (401) for {what}"
    if status == 403:
        return f"Secret Server says this account is not allowed (403) to read {what}"
    if status == 404:
        return f"Secret Server has not found (404) {what}"
    if status >= 500:
        return f"Secret Server failed ({status}) answering for {what}"
    return f"Secret Server answered {status} for {what}"


class Client:
    PAGE_SIZE = 50
    MAX_PAGES = 20

    def __init__(self, base_url: str, *, http: httpx.Client):
        self.base, self.http = base_url.rstrip("/"), http

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        """Close the httpx client this one sends through."""
        close = getattr(self.http, "close", None)
        if callable(close):
            close()

    def _send(self, method, path, **kwargs):
        """One request, never redirected: a redirect could lead a metadata call to a value endpoint (say /summary to
        /api/v1/secrets/{id}), so a 3xx, or an answer for any other path, is an error and nothing more is fetched."""
        url = self.base + path
        failure = None
        try:
            r = self.http.request(method, url, timeout=TIMEOUT, follow_redirects=False, **kwargs)
        except httpx.HTTPError as exc:  # never the exception's text, nor the exception itself: it may carry the request
            failure = type(exc).__name__
        if failure is not None:  # raised outside the except block, so the httpx exception is not its __context__
            raise SecretServerError(f"Secret Server could not be reached: {failure}")
        if r.is_redirect or 300 <= r.status_code < 400:
            raise SecretServerError(f"Secret Server answered a redirect ({r.status_code}) for {path}: redirects are never followed")
        if r.url.path != httpx.URL(url).path or r.request.url.path != httpx.URL(url).path:
            raise SecretServerError(f"Secret Server answered for another path than {path}")
        return r

    def _get(self, token, path, params=None):
        return self._send("GET", path, params=params, headers={"Authorization": f"Bearer {token}"})

    def _json(self, r, what):
        if r.status_code != 200:
            raise SecretServerError(_status_problem(r.status_code, what))
        try:
            return r.json()
        except ValueError:
            raise SecretServerError(f"Secret Server's answer for {what} is not JSON") from None

    def token(self, username: str, password: str) -> str:
        """An access token for this account: the OAuth password grant, a form body."""
        r = self._send("POST", "/oauth2/token", data={"grant_type": "password", "username": username, "password": password})
        if r.status_code in (400, 401):
            raise SecretServerError(f"Secret Server refused the sign-in ({r.status_code})")
        doc = self._json(r, "the token")
        if not isinstance(doc, dict) or not isinstance(doc.get("access_token"), str) or not doc["access_token"]:
            raise SecretServerError("Secret Server's token answer has no access_token")
        return doc["access_token"]

    def _one_page(self, token, path, params, skip, take):
        """(records, whether there are more) for one page of a paged search. confirm against RestApiDocs: skip, take and hasNext."""
        doc = self._json(self._get(token, path, {**params, "skip": skip, "take": take}), path)
        records = doc.get("records") if isinstance(doc, dict) else None
        if not isinstance(records, list):
            raise SecretServerError(f"Secret Server's answer for {path} has no records")
        if not all(isinstance(rec, dict) for rec in records):
            raise SecretServerError(f"Secret Server's answer for {path} has a record that is not an object")
        return records, doc.get("hasNext") is True

    def _page(self, token, path, params):
        """Every record of a paged search."""
        out, skip = [], 0
        for _ in range(self.MAX_PAGES):
            records, more = self._one_page(token, path, params, skip, self.PAGE_SIZE)
            out += records
            if not more:
                return out
            skip += len(records) or self.PAGE_SIZE
        raise SecretServerError(f"{path} returned more than {self.MAX_PAGES} pages: narrow the search")

    @staticmethod
    def _search_params(text, folder_id):
        params = {"filter.searchText": text, "filter.includeRestricted": "true"}
        if folder_id is not None:
            params["filter.folderId"] = int(folder_id)
        return params

    @staticmethod
    def _summary_of(rec, what="a search record") -> SecretSummary:
        try:
            return SecretSummary(int(rec["id"]), str(rec["name"]), int(rec["folderId"]), int(rec["secretTemplateId"]),
                                 str(rec.get("secretTemplateName") or ""))
        except (KeyError, TypeError, ValueError):
            raise SecretServerError(f"{what} is missing id, name, folderId or secretTemplateId") from None

    def search_secrets(self, token, *, text: str = "", folder_id: int | None = None) -> list[SecretSummary]:
        """The secrets the token's account can see, by name text and optionally one folder."""
        return [self._summary_of(rec) for rec in self._page(token, "/api/v1/secrets", self._search_params(text, folder_id))]

    def search_first(self, token, *, text: str = "", folder_id: int | None = None, take: int) -> tuple[list[SecretSummary], bool]:
        """(the first `take` matching secrets the token's account can see, whether there are more): one page, for display."""
        records, more = self._one_page(token, "/api/v1/secrets", self._search_params(text, folder_id), 0, int(take))
        return [self._summary_of(rec) for rec in records[:int(take)]], more or len(records) > int(take)

    def folders(self, token, *, text: str = "") -> dict[int, str]:
        """{folder id: its path (or name)} for the folders the token's account can see."""
        out = {}
        for rec in self._page(token, "/api/v1/folders", {"filter.searchText": text}):
            try:
                name = next((rec[k] for k in ("folderPath", "folderName") if isinstance(rec.get(k), str) and rec[k]), None)
                out[int(rec["id"])] = name if name is not None else str(int(rec["id"]))
            except (KeyError, TypeError, ValueError):
                raise SecretServerError("a folder record has no id") from None
        return out

    def template_fields(self, token, template_id: int) -> list[Field]:
        """The fields a secret of this template has, without file fields. confirm against RestApiDocs: fields[].fieldSlugName."""
        doc = self._json(self._get(token, f"/api/v1/secret-templates/{int(template_id)}"), "the secret template")
        fields = doc.get("fields") if isinstance(doc, dict) else None
        if not isinstance(fields, list):
            raise SecretServerError("the secret template has no fields")
        return [Field(f["fieldSlugName"], f["displayName"] if isinstance(f.get("displayName"), str) and f["displayName"]
                      else f["fieldSlugName"], f.get("isPassword") is True)
                for f in fields if isinstance(f, dict) and f.get("isFile") is False and isinstance(f.get("fieldSlugName"), str)
                and f["fieldSlugName"]]

    def _summary(self, token, secret_id: int) -> dict | None:
        """The secret's summary as the token's account sees it, or None when that account can't see it (403 or 404).
        confirm against RestApiDocs: GET /api/v1/secrets/{id}/summary. Anything else is an error, never "can see"."""
        r = self._get(token, f"/api/v1/secrets/{int(secret_id)}/summary")
        if r.status_code in (403, 404):
            return None
        doc = self._json(r, "the secret's summary")
        if not isinstance(doc, dict) or doc.get("id") != int(secret_id):
            raise SecretServerError("Secret Server's summary is not of the secret asked for")
        return doc

    def secret_summary(self, token, secret_id: int) -> SecretSummary | None:
        """The secret as the caller (the token's account) sees it, or None when the caller can't see it."""
        doc = self._summary(token, secret_id)
        return None if doc is None else self._summary_of(doc, "the secret's summary")

    def shared_with_platform(self, platform_token, secret_id: int) -> bool:
        """Whether the platform's account (golden-road-eso) can see the secret: its summary answers for it."""
        return self._summary(platform_token, secret_id) is not None
