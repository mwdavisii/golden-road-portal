"""The portal's configuration, read once at startup from the environment. Anything missing or malformed stops the process."""

import dataclasses as dc
import datetime as dt
import json
import re
from urllib.parse import urlsplit

from .roles import ROLE_NAMES

ORG = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")
CLAIM = re.compile(r"[A-Za-z0-9_:.-]{1,64}")
ALGORITHMS = frozenset({"RS256", "PS256", "ES256"})  # never none or an HMAC algorithm


class SettingsError(Exception):
    pass


def github_bases(host: str) -> tuple[str, str]:
    """(web, API) base URLs of a GitHub host: github.com, a ghe.com data-residency tenant, or GitHub Enterprise Server."""
    if host == "github.com":
        return "https://github.com", "https://api.github.com"
    if host.endswith(".ghe.com"):
        return f"https://{host}", f"https://api.{host}"
    return f"https://{host}", f"https://{host}/api/v3"


def _https(name: str, value: str, *, root_only: bool = False) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise SettingsError(f"{name} must be an https URL with a host and no user info, query or fragment")
    if root_only:
        if parts.path not in ("", "/"):
            raise SettingsError(f"{name} must have no path: the session cookie is a __Host- cookie on /")
        try:
            port = parts.port
        except ValueError as exc:
            raise SettingsError(f"{name} has a bad port") from exc
        # The Origin header a browser sends is lowercase and has no default port, and is compared as written.
        return f"https://{parts.hostname}" + (f":{port}" if port not in (None, 443) else "")
    return value


def parse_role_groups(text: str) -> dict:
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise SettingsError("PORTAL_ROLE_GROUPS is not JSON") from exc
    if not isinstance(doc, dict) or "admin" not in doc or set(doc) - set(ROLE_NAMES):
        raise SettingsError("PORTAL_ROLE_GROUPS must map admin (and optionally ops) to lists of group names")
    out = {}
    for role, groups in doc.items():
        if not isinstance(groups, list) or not groups or not all(isinstance(g, str) and g.strip() == g and g for g in groups):
            raise SettingsError(f"PORTAL_ROLE_GROUPS {role} must be a non-empty list of group names")
        out[role] = frozenset(groups)
    return out


@dc.dataclass(frozen=True)
class Settings:
    base_url: str
    org: str
    github_host: str
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str
    role_groups: dict
    github_oauth_client_id: str
    github_oauth_client_secret: str
    database_url: str
    oidc_scopes: str = "openid profile email"
    oidc_groups_claim: str = "groups"
    oidc_algorithms: frozenset = frozenset({"RS256"})
    session_idle: dt.timedelta = dt.timedelta(hours=1)
    session_max: dt.timedelta = dt.timedelta(hours=8)
    review_months: int = 12
    repo_admins_stale_after: dt.timedelta = dt.timedelta(minutes=10)  # two collector intervals of 5 minutes

    @property
    def origin(self) -> str:
        return self.base_url

    @property
    def github_web(self) -> str:
        return github_bases(self.github_host)[0]

    @property
    def github_api(self) -> str:
        return github_bases(self.github_host)[1]

    @classmethod
    def from_env(cls, env) -> "Settings":
        settings, problems = cls.check(env)
        if problems:
            raise SettingsError(next(iter(problems.values())))
        return settings

    @classmethod
    def check(cls, env) -> tuple["Settings | None", dict]:
        """(settings, {}) when the environment is complete and valid; (None, {setting name: reason}) naming every setting that is
        missing or invalid. The names are fixed in this code; no value is ever read into a reason."""
        problems = {}

        def get(name, check, default=None):
            try:
                return check(env.get(name, "") if default is None else env.get(name, default))
            except SettingsError as exc:
                problems.setdefault(name, str(exc))

        def need(name, check=lambda v: v):
            def run(value):
                if not value:
                    raise SettingsError(f"{name} is not set")
                return check(value)
            return get(name, run)

        def org_ok(value):
            if not ORG.fullmatch(value) or len(value) > 39:
                raise SettingsError("PORTAL_ORG is not a GitHub organization name")
            return value

        def host_ok(value):
            value = value.lower()
            if not HOST.fullmatch(value):
                raise SettingsError("PORTAL_GITHUB_HOST is not a host name")
            return value

        def algorithms_ok(value):
            found = frozenset(a.strip() for a in value.split(",") if a.strip())
            if not found or not found <= ALGORITHMS:
                raise SettingsError(f"PORTAL_OIDC_ALGORITHMS must be some of {', '.join(sorted(ALGORITHMS))}")
            return found

        def claim_ok(value):
            if not CLAIM.fullmatch(value):
                raise SettingsError("PORTAL_OIDC_GROUPS_CLAIM is not a claim name")
            return value

        def months_ok(value):
            try:
                months = int(value)
            except ValueError as exc:
                raise SettingsError("PORTAL_REVIEW_MONTHS must be a whole number") from exc
            if not 1 <= months <= 36:
                raise SettingsError("PORTAL_REVIEW_MONTHS must be from 1 to 36")
            return months

        def stale_ok(value):
            try:
                stale = int(value)
            except ValueError as exc:
                raise SettingsError("PORTAL_REPO_ADMINS_STALE_AFTER must be a whole number of seconds") from exc
            if not 60 <= stale <= 7 * 86400:
                raise SettingsError("PORTAL_REPO_ADMINS_STALE_AFTER must be from 60 seconds to 7 days")
            return stale

        values = dict(
            base_url=need("PORTAL_BASE_URL", lambda v: _https("PORTAL_BASE_URL", v, root_only=True)),
            org=need("PORTAL_ORG", org_ok), github_host=need("PORTAL_GITHUB_HOST", host_ok),
            oidc_issuer=need("PORTAL_OIDC_ISSUER", lambda v: _https("PORTAL_OIDC_ISSUER", v)),
            oidc_client_id=need("PORTAL_OIDC_CLIENT_ID"), oidc_client_secret=need("PORTAL_OIDC_CLIENT_SECRET"),
            role_groups=need("PORTAL_ROLE_GROUPS", parse_role_groups),
            github_oauth_client_id=need("PORTAL_GITHUB_OAUTH_CLIENT_ID"),
            github_oauth_client_secret=need("PORTAL_GITHUB_OAUTH_CLIENT_SECRET"), database_url=need("PORTAL_DATABASE_URL"),
            oidc_scopes=env.get("PORTAL_OIDC_SCOPES", "openid profile email"),
            oidc_groups_claim=get("PORTAL_OIDC_GROUPS_CLAIM", claim_ok, "groups"),
            oidc_algorithms=get("PORTAL_OIDC_ALGORITHMS", algorithms_ok, "RS256"),
            review_months=get("PORTAL_REVIEW_MONTHS", months_ok, "12"))
        stale = get("PORTAL_REPO_ADMINS_STALE_AFTER", stale_ok, "600")
        if problems:
            return None, problems
        return cls(**values, repo_admins_stale_after=dt.timedelta(seconds=stale)), {}
