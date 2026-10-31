"""GitHub App authentication: a short RS256 JWT for the App, exchanged for an installation token (read or write installation)."""

import datetime as dt
import time

import jwt


def app_jwt(app_id: int, private_key_pem: bytes, now: int) -> str:
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}, private_key_pem, algorithm="RS256")


class InstallationToken:
    """A callable that returns a valid installation token, fetching a new one five minutes before the old one expires. It keeps the
    permissions GitHub granted the last token (`permissions`), so the collector can tell whether it may read Actions."""

    def __init__(self, http, api_base, app_id, private_key_pem, installation_id, clock=time.time):
        self.http, self.api_base, self.app_id, self.key, self.installation_id, self.clock = http, api_base.rstrip("/"), app_id, private_key_pem, installation_id, clock
        self._token, self._expires = None, 0.0
        self.permissions = {}

    def __call__(self) -> str:
        now = self.clock()
        if self._token and now < self._expires - 300:
            return self._token
        from .client import GitHub
        app = GitHub(self.api_base, self.http, lambda: app_jwt(self.app_id, self.key, int(now)))
        doc = app.call("POST", f"/app/installations/{self.installation_id}/access_tokens", ok=(201,)).json()
        self._token = doc["token"]
        self._expires = dt.datetime.strptime(doc["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC).timestamp()
        granted = doc.get("permissions")
        self.permissions = {k: v for k, v in granted.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(granted, dict) else {}
        return self._token
