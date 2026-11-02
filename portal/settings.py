"""The portal's configuration, read once at startup from the environment. Anything missing or malformed stops the process."""

import dataclasses as dc
import datetime as dt
import json
import re
from urllib.parse import urlsplit

from .classification import POLICY_LINK_MAX, policy_link_ok
from .roles import ROLE_NAMES

ORG = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")
CLAIM = re.compile(r"[A-Za-z0-9_:.-]{1,64}")
ALGORITHMS = frozenset({"RS256", "PS256", "ES256"})  # never none or an HMAC algorithm
# builder.config's GATEWAY: deploy-verify holds bindings.yaml's mcp.gateway to platform.json's host, checked the same way
GATEWAY = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")
DEFAULT_GATEWAYS = {"test": "mcp-test.example.com", "prod": "mcp.example.com"}  # platform.json's placeholders
GATEWAY_SETTINGS = {"test": "PORTAL_MCP_GATEWAY_TEST", "prod": "PORTAL_MCP_GATEWAY_PROD"}


class SettingsError(Exception):
    pass


def github_bases(host: str) -> tuple[str, str]:
    """(web, API) base URLs of a GitHub host: github.com, a ghe.com data-residency tenant, or GitHub Enterprise Server."""
    if host == "github.com":
        return "https://github.com", "https://api.github.com"
    if host.endswith(".ghe.com"):
        return f"https://{host}", f"https://api.{host}"
    return f"https://{host}", f"https://{host}/api/v3"


def _https(name: str, value: str, *, root_only: bool = False, query: bool = False) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or (parts.query and not query) \
            or parts.fragment or "#" in value:
        raise SettingsError(f"{name} must be an https URL with a host and no user info, {'' if query else 'query or '}fragment")
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


def no_secret_server_url(value: str) -> None:
    """PORTAL_SECRET_SERVER_URL is the spec's setting for a real Secret Server. There is only the mock, so it must stay unset: set,
    it would look as if a real instance were in use. Shared by the web and the writer."""
    if value:
        raise SettingsError("PORTAL_SECRET_SERVER_URL is for a real Secret Server, which this portal does not have yet; leave it "
                            "unset (PORTAL_SECRET_SERVER=mock needs no URL)")


def mcp_gateway(name: str, value: str, default: str) -> str:
    """One environment's shared Cloudflare MCP gateway host (PORTAL_MCP_GATEWAY_TEST or _PROD): empty is its default, platform.json's
    placeholder. It must be the host configure.py recorded in platform.json's mcp_gateways, or deploy-check fails the binding."""
    value = (value or default).lower()
    if len(value) > 253 or not GATEWAY.fullmatch(value):
        raise SettingsError(f"{name} is not a host name (the environment's shared Cloudflare MCP gateway, as in platform.json)")
    return value


def distinct_gateways(gateways: dict) -> None:
    """Each environment has its own gateway, as platform.json's mcp_gateways must. The problem is PORTAL_MCP_GATEWAY_PROD's."""
    if gateways["test"] == gateways["prod"]:
        raise SettingsError("PORTAL_MCP_GATEWAY_TEST and PORTAL_MCP_GATEWAY_PROD must differ: each environment has its own gateway")


def mcp_gateways_from(env) -> dict:
    """{"test": host, "prod": host} from PORTAL_MCP_GATEWAY_TEST and _PROD, each empty for its default. Shared by the web and the
    writer."""
    gateways = {e: mcp_gateway(name, env.get(name, ""), DEFAULT_GATEWAYS[e]) for e, name in GATEWAY_SETTINGS.items()}
    distinct_gateways(gateways)
    return gateways


def cloudflare_from(value: str, gateways: dict):
    """PORTAL_CLOUDFLARE: unset is None (the app page has no MCP servers), mock is portal.cloudflare_mock serving these gateway hosts.
    Shared by the web and the writer."""
    if value == "":
        return None
    if value != "mock":
        raise SettingsError("PORTAL_CLOUDFLARE must be mock or unset (the only Cloudflare there is is the mock)")
    from .cloudflare_mock import MockCloudflareError, load_mock
    try:
        return load_mock(gateways=gateways)
    except MockCloudflareError:
        raise SettingsError("PORTAL_CLOUDFLARE is mock, but the mock Cloudflare is not valid") from None


def no_cloudflare_account(value: str) -> None:
    """PORTAL_CLOUDFLARE_ACCOUNT_ID is the setting for a real Cloudflare account. There is only the mock, so it must stay unset: set,
    it would look as if a real account were in use. Shared by the web and the writer."""
    if value:
        raise SettingsError("PORTAL_CLOUDFLARE_ACCOUNT_ID is for a real Cloudflare account, which this portal does not have yet; "
                            "leave it unset (PORTAL_CLOUDFLARE=mock needs no account)")


def no_servicenow_url(value: str) -> None:
    """PORTAL_SERVICENOW_URL is the setting for a real ServiceNow instance. There is only the mock, so it must stay unset: set, it
    would look as if a real instance were in use. Shared by the web and the collector."""
    if value:
        raise SettingsError("PORTAL_SERVICENOW_URL is for a real ServiceNow instance, which this portal does not have yet; leave it "
                            "unset (PORTAL_SERVICENOW=mock needs no URL)")


def servicenow_from(value: str):
    """PORTAL_SERVICENOW: unset is None (no CMDB sync, no CMDB card), mock is portal.servicenow_mock over mock_servicenow.json.
    Shared by the web and the collector."""
    if value == "":
        return None
    if value != "mock":
        raise SettingsError("PORTAL_SERVICENOW must be mock or unset (the only ServiceNow there is is the mock)")
    from .servicenow_mock import MockServiceNowError, load_mock
    try:
        return load_mock()
    except MockServiceNowError:
        raise SettingsError("PORTAL_SERVICENOW is mock, but the mock ServiceNow is not valid") from None


def servicenow_on(value: str) -> bool:
    """PORTAL_SERVICENOW for the web: mock or unset, as the collector reads it. The web never calls ServiceNow; on, it shows what the
    collector recorded (the app page's CMDB card, the timeline's step)."""
    if value not in ("", "mock"):
        raise SettingsError("PORTAL_SERVICENOW must be mock or unset (the only ServiceNow there is is the mock)")
    return value == "mock"


DEFAULT_ORG_NAME = "the organization"
ORG_NAME_MAX = 100


def org_name_ok(value: str) -> str:
    """PORTAL_ORG_NAME: the organization's name in the ownership notice ("If you leave {org} ..."). One line of 1 to 100 characters
    with no control characters; empty is "the organization"."""
    if value == "":
        return DEFAULT_ORG_NAME
    if not value.strip() or len(value) > ORG_NAME_MAX or any(ord(c) < 32 or 0x7f <= ord(c) < 0xa0 or c in "\u2028\u2029" for c in value):
        raise SettingsError(f"PORTAL_ORG_NAME must be one line of 1 to {ORG_NAME_MAX} characters with no control characters")
    return value.strip()


def grace_days_ok(value: str) -> int:
    """PORTAL_LIFECYCLE_GRACE_DAYS: the days the ownership notice gives an owner to confirm, or to name a new owner, before the app is
    decommissioned. Empty is 30. 0 hides the day count ("in time"); it never means "no time" (no budgets)."""
    from .rereview import MAX_DAYS, BadSetting, parse_days
    try:
        return parse_days("PORTAL_LIFECYCLE_GRACE_DAYS", value)
    except BadSetting:
        raise SettingsError(f"PORTAL_LIFECYCLE_GRACE_DAYS must be a whole number of days from 0 to {MAX_DAYS} (0 leaves the day count "
                            "out of the ownership notice; it never means no time)") from None


def skills_catalog_from(value: str):
    """PORTAL_SKILLS_CATALOG: unset is None (the app page has no skills), mock is portal/mock_skills_catalog.json, the fixture
    deploy-verify reads. Shared by the web and the writer."""
    if value == "":
        return None
    if value != "mock":
        raise SettingsError("PORTAL_SKILLS_CATALOG must be mock or unset (the only skills catalog there is is the mock)")
    from .skills_catalog import SkillsCatalogError, load_mock
    try:
        return load_mock()
    except SkillsCatalogError:
        raise SettingsError("PORTAL_SKILLS_CATALOG is mock, but the mock skills catalog is not valid") from None


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
    directory: object = None  # portal.directory.Directory when PORTAL_DIRECTORY is set; None hides the app's connections
    identity_process_url: str = ""  # where security's process for service principals is described (link text on the app page)
    data_process_url: str = ""  # where analytics' process for warehouse grants is described
    secret_server: object = None  # portal.secretserver_mock.MockSecretServer when PORTAL_SECRET_SERVER=mock; None hides app secrets
    secret_process_url: str = ""  # where security's process for sharing a secret with golden-road-eso is described
    classification_policy_url: str = ""  # PORTAL_CLASSIFICATION_POLICY_URL: the data classification policy; empty hides the link
    rereview_stale_days: int = 30  # PORTAL_REREVIEW_STALE_DAYS: an item open longer is flagged rereview_stale; 0 turns the flag off
    cloudflare: object = None  # portal.cloudflare_mock.MockCloudflare when PORTAL_CLOUDFLARE=mock; None hides the app's MCP servers
    mcp_gateways: dict = dc.field(default_factory=lambda: dict(DEFAULT_GATEWAYS))  # PORTAL_MCP_GATEWAY_TEST/_PROD: {env: host}
    mcp_process_url: str = ""  # where the platform's process for adding an MCP server to a gateway is described
    skills_catalog: object = None  # portal.skills_catalog.MockSkillsCatalog when PORTAL_SKILLS_CATALOG=mock; None hides app skills
    servicenow: bool = False  # PORTAL_SERVICENOW=mock: show the collector's CMDB records (the app page's card, the timeline's step)
    org_name: str = DEFAULT_ORG_NAME  # PORTAL_ORG_NAME: named in the ownership notice
    lifecycle_grace_days: int = 30  # PORTAL_LIFECYCLE_GRACE_DAYS: the notice's day count; 0 hides it (never "no time")

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
            if value == "":
                value = "600"  # empty (the kit's config map leaves it so) is the default, as the writer reads it
            try:
                stale = int(value)
            except ValueError as exc:
                raise SettingsError("PORTAL_REPO_ADMINS_STALE_AFTER must be a whole number of seconds") from exc
            if not 60 <= stale <= 7 * 86400:
                raise SettingsError("PORTAL_REPO_ADMINS_STALE_AFTER must be from 60 seconds to 7 days")
            return stale

        def days_ok(name):
            def run(value):
                from .rereview import BadSetting, parse_days
                try:
                    return parse_days(name, value)
                except BadSetting as exc:
                    raise SettingsError(str(exc)) from None
            return run

        def directory_ok(value):
            if value == "":
                return None
            if value != "mock":
                raise SettingsError("PORTAL_DIRECTORY must be mock or unset (the only directory there is is the mock)")
            from .directory import DirectoryError, load_mock
            try:
                return load_mock()
            except DirectoryError as exc:
                raise SettingsError("PORTAL_DIRECTORY is mock, but the mock directory is not valid") from exc

        def secret_server_ok(value):
            if value == "":
                return None
            if value != "mock":
                raise SettingsError("PORTAL_SECRET_SERVER must be mock or unset (the only Secret Server there is is the mock)")
            from .secretserver_mock import MockSecretServerError, load_mock as load_secret_server
            try:
                return load_secret_server()
            except MockSecretServerError as exc:
                raise SettingsError("PORTAL_SECRET_SERVER is mock, but the mock Secret Server is not valid") from exc

        def policy_ok(value):  # the one rule for the policy link, as configure.py and the builder apply it (contracts.classification)
            if value and not policy_link_ok(value):
                raise SettingsError(f"PORTAL_CLASSIFICATION_POLICY_URL must be an https URL with a host name, at most {POLICY_LINK_MAX} "
                                    "characters, and no user info, spaces, quotes or brackets")
            return value

        def link_ok(name):
            return lambda value: _https(name, value, query=True) if value else ""  # a wiki page link may need its query

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
            review_months=get("PORTAL_REVIEW_MONTHS", months_ok, "12"),
            directory=get("PORTAL_DIRECTORY", directory_ok, ""),
            identity_process_url=get("PORTAL_IDENTITY_PROCESS_URL", link_ok("PORTAL_IDENTITY_PROCESS_URL"), ""),
            data_process_url=get("PORTAL_DATA_PROCESS_URL", link_ok("PORTAL_DATA_PROCESS_URL"), ""),
            secret_server=get("PORTAL_SECRET_SERVER", secret_server_ok, ""),
            secret_process_url=get("PORTAL_SECRET_PROCESS_URL", link_ok("PORTAL_SECRET_PROCESS_URL"), ""),
            classification_policy_url=get("PORTAL_CLASSIFICATION_POLICY_URL", policy_ok, ""),
            rereview_stale_days=get("PORTAL_REREVIEW_STALE_DAYS", days_ok("PORTAL_REREVIEW_STALE_DAYS"), ""))
        get("PORTAL_SECRET_SERVER_URL", no_secret_server_url, "")
        gateways = {e: get(name, lambda v, name=name, e=e: mcp_gateway(name, v, DEFAULT_GATEWAYS[e]), "")
                    for e, name in GATEWAY_SETTINGS.items()}
        if None not in gateways.values():
            get("PORTAL_MCP_GATEWAY_PROD", lambda _: distinct_gateways(gateways), "")
        # with a bad gateway the mock still loads on the placeholders, so a bad PORTAL_CLOUDFLARE is named too (startup stops anyway)
        good_gateways = gateways if not {"PORTAL_MCP_GATEWAY_TEST", "PORTAL_MCP_GATEWAY_PROD"} & set(problems) else DEFAULT_GATEWAYS
        values.update(cloudflare=get("PORTAL_CLOUDFLARE", lambda v: cloudflare_from(v, good_gateways), ""), mcp_gateways=gateways,
                      mcp_process_url=get("PORTAL_MCP_PROCESS_URL", link_ok("PORTAL_MCP_PROCESS_URL"), ""),
                      skills_catalog=get("PORTAL_SKILLS_CATALOG", skills_catalog_from, ""))
        get("PORTAL_CLOUDFLARE_ACCOUNT_ID", no_cloudflare_account, "")
        values.update(servicenow=get("PORTAL_SERVICENOW", servicenow_on, ""), org_name=get("PORTAL_ORG_NAME", org_name_ok, ""),
                      lifecycle_grace_days=get("PORTAL_LIFECYCLE_GRACE_DAYS", grace_days_ok, ""))
        get("PORTAL_SERVICENOW_URL", no_servicenow_url, "")
        stale = get("PORTAL_REPO_ADMINS_STALE_AFTER", stale_ok, "600")
        if problems:
            return None, problems
        return cls(**values, repo_admins_stale_after=dt.timedelta(seconds=stale)), {}
