import datetime as dt

import pytest

from portal.roles import roles_for
from portal.settings import Settings, SettingsError, github_bases, parse_role_groups

ENV = {"PORTAL_BASE_URL": "https://portal.example.test", "PORTAL_ORG": "acme", "PORTAL_GITHUB_HOST": "github.com",
       "PORTAL_OIDC_ISSUER": "https://sso.example.test/application/o/portal/", "PORTAL_OIDC_CLIENT_ID": "portal-client",
       "PORTAL_OIDC_CLIENT_SECRET": "s", "PORTAL_ROLE_GROUPS": '{"admin": ["golden-road-admins"], "ops": ["golden-road-ops"]}',
       "PORTAL_GITHUB_OAUTH_CLIENT_ID": "Iv1.x", "PORTAL_GITHUB_OAUTH_CLIENT_SECRET": "s", "PORTAL_DATABASE_URL": "postgresql://x"}


def test_settings_from_a_complete_environment():
    s = Settings.from_env(ENV)
    assert (s.base_url, s.origin, s.org, s.github_web, s.github_api) == (
        "https://portal.example.test", "https://portal.example.test", "acme", "https://github.com", "https://api.github.com")
    assert s.role_groups == {"admin": frozenset({"golden-road-admins"}), "ops": frozenset({"golden-road-ops"})}
    assert (s.oidc_algorithms, s.oidc_groups_claim, s.session_idle, s.session_max, s.review_months) == (
        frozenset({"RS256"}), "groups", dt.timedelta(hours=1), dt.timedelta(hours=8), 12)


@pytest.mark.parametrize("change", [
    {"PORTAL_BASE_URL": "http://portal.example.test"}, {"PORTAL_BASE_URL": "https://portal.example.test/sub"},
    {"PORTAL_BASE_URL": "https://user@portal.example.test"}, {"PORTAL_OIDC_ISSUER": "http://sso.example.test"},
    {"PORTAL_OIDC_ISSUER": "https://sso.example.test/?x=1"}, {"PORTAL_ORG": "bad org"}, {"PORTAL_GITHUB_HOST": "not a host"},
    {"PORTAL_ROLE_GROUPS": "[]"}, {"PORTAL_ROLE_GROUPS": '{"ops": ["g"]}'}, {"PORTAL_ROLE_GROUPS": '{"admin": []}'},
    {"PORTAL_ROLE_GROUPS": '{"admin": ["g"], "owner": ["h"]}'}, {"PORTAL_OIDC_ALGORITHMS": "HS256"},
    {"PORTAL_OIDC_ALGORITHMS": "none"}, {"PORTAL_REVIEW_MONTHS": "0"}, {"PORTAL_DATABASE_URL": ""},
])
def test_a_bad_or_missing_setting_stops_startup(change):
    with pytest.raises(SettingsError):
        Settings.from_env({**ENV, **change})


def test_github_hosts():
    assert github_bases("github.com") == ("https://github.com", "https://api.github.com")
    assert github_bases("acme.ghe.com") == ("https://acme.ghe.com", "https://api.acme.ghe.com")
    assert github_bases("git.example.org") == ("https://git.example.org", "https://git.example.org/api/v3")


def test_roles_come_from_the_groups_claim_and_contributor_is_implicit():
    groups = parse_role_groups('{"admin": ["golden-road-admins"], "ops": ["golden-road-ops"]}')
    assert roles_for([], groups) == ()
    assert roles_for(["golden-road-ops", "other"], groups) == ("ops",)
    assert roles_for(["golden-road-ops", "golden-road-admins"], groups) == ("admin", "ops")
    assert roles_for(["Golden-Road-Admins"], groups) == ()  # exact match: the identity provider's groups decide (P15)


@pytest.mark.parametrize("given, origin", [
    ("https://Portal.Example.TEST", "https://portal.example.test"), ("https://portal.example.test:443", "https://portal.example.test"),
    ("https://PORTAL.example.test:443/", "https://portal.example.test"), ("https://portal.example.test:8443", "https://portal.example.test:8443"),
])
def test_the_origin_is_normalized_the_way_a_browser_sends_it(given, origin):
    s = Settings.from_env({**ENV, "PORTAL_BASE_URL": given})
    assert s.origin == origin and s.base_url == origin


def test_the_repo_admin_observation_goes_stale_after_two_collector_intervals_by_default():
    s = Settings.from_env(ENV)
    assert s.repo_admins_stale_after == dt.timedelta(minutes=10)
    assert Settings.from_env({**ENV, "PORTAL_REPO_ADMINS_STALE_AFTER": "1800"}).repo_admins_stale_after == dt.timedelta(minutes=30)
    # empty (the kit's config map leaves it so) is the default, as the writer reads it: the two can't differ
    assert Settings.from_env({**ENV, "PORTAL_REPO_ADMINS_STALE_AFTER": ""}).repo_admins_stale_after == dt.timedelta(minutes=10)


@pytest.mark.parametrize("value", ["59", "0", "-5", "soon", "700000"])
def test_a_bad_staleness_limit_stops_startup(value):
    with pytest.raises(SettingsError):
        Settings.from_env({**ENV, "PORTAL_REPO_ADMINS_STALE_AFTER": value})


# ---- the app bindings' directory and process links (spec 2026-10-29 app bindings) ----

def test_the_directory_is_off_unless_set():
    s, problems = Settings.check(ENV)
    assert problems == {} and s.directory is None and (s.identity_process_url, s.data_process_url) == ("", "")


def test_portal_directory_mock_loads_the_shipped_directory():
    s, problems = Settings.check({**ENV, "PORTAL_DIRECTORY": "mock"})
    assert problems == {} and s.directory is not None and s.directory.name == "mock-directory"


def test_an_unknown_directory_is_refused():
    assert "PORTAL_DIRECTORY" in Settings.check({**ENV, "PORTAL_DIRECTORY": "azure"})[1]


def test_a_bad_mock_directory_file_stops_startup(tmp_path, monkeypatch):
    import portal.directory
    (tmp_path / "mock.json").write_text('{"schema": "mock-directory/v1"}')
    monkeypatch.setattr(portal.directory, "MOCK", tmp_path / "mock.json")
    s, problems = Settings.check({**ENV, "PORTAL_DIRECTORY": "mock"})
    assert s is None and set(problems) == {"PORTAL_DIRECTORY"}
    monkeypatch.setattr(portal.directory, "MOCK", tmp_path / "missing.json")
    with pytest.raises(SettingsError):
        Settings.from_env({**ENV, "PORTAL_DIRECTORY": "mock"})


@pytest.mark.parametrize("name", ["PORTAL_IDENTITY_PROCESS_URL", "PORTAL_DATA_PROCESS_URL"])
def test_process_links_must_be_https(name):
    assert name in Settings.check({**ENV, name: "http://example.test/x"})[1]
    s, problems = Settings.check({**ENV, name: "https://wiki.example.test/identity"})
    assert problems == {} and "https://wiki.example.test/identity" in (s.identity_process_url, s.data_process_url)


@pytest.mark.parametrize("name", ["PORTAL_IDENTITY_PROCESS_URL", "PORTAL_DATA_PROCESS_URL"])
def test_process_links_may_carry_a_query_but_not_a_fragment(name):
    s, problems = Settings.check({**ENV, name: "https://wiki.example.test/page?id=42"})
    assert problems == {} and "https://wiki.example.test/page?id=42" in (s.identity_process_url, s.data_process_url)
    assert name in Settings.check({**ENV, name: "https://wiki.example.test/page#identity"})[1]
    assert name in Settings.check({**ENV, name: "https://user@wiki.example.test/page"})[1]


# ---- Delinea Secret Server (spec 2026-10-29 Delinea secrets): mock or unset, and security's process link ----

def test_secret_server_is_off_unless_set():
    s, problems = Settings.check(ENV)
    assert problems == {} and s.secret_server is None and s.secret_process_url == ""
    s, problems = Settings.check({**ENV, "PORTAL_SECRET_SERVER": "", "PORTAL_SECRET_PROCESS_URL": ""})
    assert problems == {} and s.secret_server is None and s.secret_process_url == ""


def test_portal_secret_server_mock_loads_the_shipped_mock():
    from portal.secretserver_mock import MockSecretServer
    s, problems = Settings.check({**ENV, "PORTAL_SECRET_SERVER": "mock"})
    assert problems == {} and isinstance(s.secret_server, MockSecretServer) and s.secret_server.platform_user == "golden-road-eso"


@pytest.mark.parametrize("value", ["delinea", "Mock", "mock\n", "real", "https://ss.example.test/SecretServer"])
def test_an_unknown_secret_server_is_refused(value):
    s, problems = Settings.check({**ENV, "PORTAL_SECRET_SERVER": value})
    assert s is None and set(problems) == {"PORTAL_SECRET_SERVER"} and value not in problems["PORTAL_SECRET_SERVER"]
    with pytest.raises(SettingsError, match="PORTAL_SECRET_SERVER"):
        Settings.from_env({**ENV, "PORTAL_SECRET_SERVER": value})


def test_a_bad_mock_secret_server_file_stops_startup(tmp_path, monkeypatch):
    import portal.secretserver_mock
    (tmp_path / "mock.json").write_text('{"schema": "mock-secret-server/v1"}')
    monkeypatch.setattr(portal.secretserver_mock, "MOCK", tmp_path / "mock.json")
    s, problems = Settings.check({**ENV, "PORTAL_SECRET_SERVER": "mock"})
    assert s is None and set(problems) == {"PORTAL_SECRET_SERVER"}
    monkeypatch.setattr(portal.secretserver_mock, "MOCK", tmp_path / "missing.json")
    with pytest.raises(SettingsError, match="PORTAL_SECRET_SERVER is mock, but the mock Secret Server is not valid"):
        Settings.from_env({**ENV, "PORTAL_SECRET_SERVER": "mock"})


@pytest.mark.parametrize("server", ["", "mock"])
def test_a_secret_server_url_is_refused_while_only_the_mock_exists(server):
    """The spec's PORTAL_SECRET_SERVER_URL is for a real instance, which there is none of: set, it would look like one is used."""
    s, problems = Settings.check({**ENV, "PORTAL_SECRET_SERVER": server, "PORTAL_SECRET_SERVER_URL": "https://ss.example.test/SecretServer"})
    assert s is None and set(problems) == {"PORTAL_SECRET_SERVER_URL"} and "ss.example.test" not in problems["PORTAL_SECRET_SERVER_URL"]
    assert Settings.check({**ENV, "PORTAL_SECRET_SERVER_URL": ""})[1] == {}


def test_the_secret_process_link_must_be_https():
    name = "PORTAL_SECRET_PROCESS_URL"
    assert name in Settings.check({**ENV, name: "http://wiki.example.test/secrets"})[1]
    assert name in Settings.check({**ENV, name: "https://wiki.example.test/page#secrets"})[1]
    assert name in Settings.check({**ENV, name: "https://user@wiki.example.test/page"})[1]
    s, problems = Settings.check({**ENV, name: "https://wiki.example.test/page?id=7"})
    assert problems == {} and s.secret_process_url == "https://wiki.example.test/page?id=7"


def test_the_classification_policy_link_is_optional_and_https():
    s, problems = Settings.check(ENV)
    assert problems == {} and s.classification_policy_url == ""
    s, problems = Settings.check({**ENV, "PORTAL_CLASSIFICATION_POLICY_URL": "https://policies.example.test/data?v=2"})
    assert problems == {} and s.classification_policy_url == "https://policies.example.test/data?v=2"
    s, problems = Settings.check({**ENV, "PORTAL_CLASSIFICATION_POLICY_URL": "https://policies.example.test/data#levels"})
    assert problems == {} and s.classification_policy_url == "https://policies.example.test/data#levels"
    for bad in ("http://policies.example.test/data", "https://user@policies.example.test/data", "policies.example.test",
                "javascript:alert(1)", "https://policies.example.test/a b", "https://policies.example.test/a\nb",
                'https://policies.example.test/"x', " https://policies.example.test/p", "https://policies.example.test/" + "a" * 2030):
        s, problems = Settings.check({**ENV, "PORTAL_CLASSIFICATION_POLICY_URL": bad})
        assert s is None and set(problems) == {"PORTAL_CLASSIFICATION_POLICY_URL"}, bad


def test_the_rereview_stale_days_default_to_30_and_0_turns_the_flag_off():
    s, problems = Settings.check(ENV)
    assert problems == {} and s.rereview_stale_days == 30
    for raw, days in (("", 30), ("0", 0), ("90", 90)):
        s, problems = Settings.check({**ENV, "PORTAL_REREVIEW_STALE_DAYS": raw})
        assert problems == {} and s.rereview_stale_days == days
    for bad in ("-1", "a month", "36501"):
        s, problems = Settings.check({**ENV, "PORTAL_REREVIEW_STALE_DAYS": bad})
        assert s is None and set(problems) == {"PORTAL_REREVIEW_STALE_DAYS"} and bad not in problems["PORTAL_REREVIEW_STALE_DAYS"]


# ---- MCP servers and skills (spec 2026-10-31): the mock Cloudflare, the two gateways, the skills catalog, the MCP process link ----

def test_cloudflare_and_the_skills_catalog_are_off_unless_set():
    s, problems = Settings.check(ENV)
    assert problems == {} and s.cloudflare is None and s.skills_catalog is None and s.mcp_process_url == ""
    assert s.mcp_gateways == {"test": "mcp-test.example.com", "prod": "mcp.example.com"}
    s, problems = Settings.check({**ENV, "PORTAL_CLOUDFLARE": "", "PORTAL_SKILLS_CATALOG": "", "PORTAL_MCP_GATEWAY_TEST": "",
                                  "PORTAL_MCP_GATEWAY_PROD": "", "PORTAL_MCP_PROCESS_URL": ""})
    assert problems == {} and s.cloudflare is None and s.skills_catalog is None
    assert s.mcp_gateways == {"test": "mcp-test.example.com", "prod": "mcp.example.com"}


def test_portal_cloudflare_mock_serves_the_default_gateways():
    from portal.cloudflare_mock import MockCloudflare
    s, problems = Settings.check({**ENV, "PORTAL_CLOUDFLARE": "mock"})
    assert problems == {} and isinstance(s.cloudflare, MockCloudflare)
    assert s.cloudflare.gateways == {"test": "mcp-test.example.com", "prod": "mcp.example.com"} == s.mcp_gateways


def test_the_configured_gateways_are_what_the_mock_serves():
    s, problems = Settings.check({**ENV, "PORTAL_CLOUDFLARE": "mock", "PORTAL_MCP_GATEWAY_TEST": "MCP-Test.Acme.example"})
    assert problems == {} and s.mcp_gateways == {"test": "mcp-test.acme.example", "prod": "mcp.example.com"} == s.cloudflare.gateways
    assert s.cloudflare.client().gateway("mcp-test.acme.example") is not None
    s, problems = Settings.check({**ENV, "PORTAL_MCP_GATEWAY_PROD": "mcp.acme.example"})
    assert problems == {} and s.mcp_gateways == {"test": "mcp-test.example.com", "prod": "mcp.acme.example"} and s.cloudflare is None


@pytest.mark.parametrize("name", ["PORTAL_MCP_GATEWAY_TEST", "PORTAL_MCP_GATEWAY_PROD"])
@pytest.mark.parametrize("value", ["two words.example", "https://mcp.example.com", "localhost", "mcp.example.com/", "-mcp.example.com",
                                   "mcp_test.example.com", "a" * 64 + ".example.com"])
def test_a_bad_gateway_host_is_refused_by_name(name, value):
    for cloudflare in ("", "mock"):
        s, problems = Settings.check({**ENV, "PORTAL_CLOUDFLARE": cloudflare, name: value})
        assert s is None and set(problems) == {name} and value not in problems[name]


def test_one_host_for_both_gateways_is_refused():
    s, problems = Settings.check({**ENV, "PORTAL_MCP_GATEWAY_TEST": "mcp.example.com"})
    assert s is None and set(problems) == {"PORTAL_MCP_GATEWAY_PROD"} and "differ" in problems["PORTAL_MCP_GATEWAY_PROD"]
    s, problems = Settings.check({**ENV, "PORTAL_MCP_GATEWAY_TEST": "mcp.acme.example", "PORTAL_MCP_GATEWAY_PROD": "MCP.acme.example"})
    assert s is None and set(problems) == {"PORTAL_MCP_GATEWAY_PROD"}


@pytest.mark.parametrize("value", ["cloudflare", "Mock", "mock\n", "real", "0123456789abcdef0123456789abcdef"])
def test_an_unknown_cloudflare_is_refused(value):
    s, problems = Settings.check({**ENV, "PORTAL_CLOUDFLARE": value})
    assert s is None and set(problems) == {"PORTAL_CLOUDFLARE"} and value not in problems["PORTAL_CLOUDFLARE"]
    with pytest.raises(SettingsError, match="PORTAL_CLOUDFLARE"):
        Settings.from_env({**ENV, "PORTAL_CLOUDFLARE": value})


def test_a_bad_mock_cloudflare_file_stops_startup(tmp_path, monkeypatch):
    import portal.cloudflare_mock
    (tmp_path / "mock.json").write_text('{"schema": "mock-cloudflare/v1"}')
    monkeypatch.setattr(portal.cloudflare_mock, "MOCK", tmp_path / "mock.json")
    s, problems = Settings.check({**ENV, "PORTAL_CLOUDFLARE": "mock"})
    assert s is None and set(problems) == {"PORTAL_CLOUDFLARE"}
    with pytest.raises(SettingsError, match="PORTAL_CLOUDFLARE is mock, but the mock Cloudflare is not valid"):
        Settings.from_env({**ENV, "PORTAL_CLOUDFLARE": "mock"})


@pytest.mark.parametrize("cloudflare", ["", "mock"])
def test_a_cloudflare_account_id_is_refused_while_only_the_mock_exists(cloudflare):
    """PORTAL_CLOUDFLARE_ACCOUNT_ID is for a real account, which there is none of: set, it would look like one is used."""
    account = "fedcba9876543210fedcba9876543210"
    s, problems = Settings.check({**ENV, "PORTAL_CLOUDFLARE": cloudflare, "PORTAL_CLOUDFLARE_ACCOUNT_ID": account})
    assert s is None and set(problems) == {"PORTAL_CLOUDFLARE_ACCOUNT_ID"} and account not in problems["PORTAL_CLOUDFLARE_ACCOUNT_ID"]
    assert "for a real Cloudflare account, which this portal does not have yet" in problems["PORTAL_CLOUDFLARE_ACCOUNT_ID"]
    assert Settings.check({**ENV, "PORTAL_CLOUDFLARE_ACCOUNT_ID": ""})[1] == {}


def test_the_mcp_process_link_must_be_https():
    name = "PORTAL_MCP_PROCESS_URL"
    assert name in Settings.check({**ENV, name: "http://wiki.example.test/mcp"})[1]
    assert name in Settings.check({**ENV, name: "https://wiki.example.test/page#mcp"})[1]
    assert name in Settings.check({**ENV, name: "https://user@wiki.example.test/page"})[1]
    s, problems = Settings.check({**ENV, name: "https://wiki.example.test/page?id=9"})
    assert problems == {} and s.mcp_process_url == "https://wiki.example.test/page?id=9"


def test_portal_skills_catalog_mock_loads_the_shipped_catalog():
    from portal.skills_catalog import MockSkillsCatalog
    s, problems = Settings.check({**ENV, "PORTAL_SKILLS_CATALOG": "mock"})
    assert problems == {} and isinstance(s.skills_catalog, MockSkillsCatalog) and s.skills_catalog.name == "mock-skills-catalog"


@pytest.mark.parametrize("value", ["git", "Mock", "mock\t", "example-org/golden-road-skills"])
def test_an_unknown_skills_catalog_is_refused(value):
    s, problems = Settings.check({**ENV, "PORTAL_SKILLS_CATALOG": value})
    assert s is None and set(problems) == {"PORTAL_SKILLS_CATALOG"} and value not in problems["PORTAL_SKILLS_CATALOG"]


def test_a_bad_mock_skills_catalog_file_stops_startup(tmp_path, monkeypatch):
    import portal.skills_catalog
    monkeypatch.setattr(portal.skills_catalog, "MOCK", tmp_path / "missing.json")
    with pytest.raises(SettingsError, match="PORTAL_SKILLS_CATALOG is mock, but the mock skills catalog is not valid"):
        Settings.from_env({**ENV, "PORTAL_SKILLS_CATALOG": "mock"})


# ---- What owning an app means (spec 2026-10-31): ServiceNow for the web, the organization's name, the grace days ----

def test_servicenow_the_org_name_and_the_grace_days_default():
    s, problems = Settings.check(ENV)
    assert problems == {} and (s.servicenow, s.org_name, s.lifecycle_grace_days) == (False, "the organization", 30)
    s, problems = Settings.check({**ENV, "PORTAL_SERVICENOW": "", "PORTAL_ORG_NAME": "", "PORTAL_LIFECYCLE_GRACE_DAYS": ""})
    assert problems == {} and (s.servicenow, s.org_name, s.lifecycle_grace_days) == (False, "the organization", 30)
    s, problems = Settings.check({**ENV, "PORTAL_SERVICENOW": "mock", "PORTAL_ORG_NAME": "Example Org", "PORTAL_LIFECYCLE_GRACE_DAYS": "0"})
    assert problems == {} and (s.servicenow, s.org_name, s.lifecycle_grace_days) == (True, "Example Org", 0)
    assert Settings.check({**ENV, "PORTAL_LIFECYCLE_GRACE_DAYS": "45"})[0].lifecycle_grace_days == 45


@pytest.mark.parametrize("value", ["Example\nOrg", "Example\rOrg", "Example\tOrg", "Example\x7fOrg", "a" * 101, "   "])
def test_a_bad_org_name_is_refused(value):
    s, problems = Settings.check({**ENV, "PORTAL_ORG_NAME": value})
    assert s is None and set(problems) == {"PORTAL_ORG_NAME"} and value not in problems["PORTAL_ORG_NAME"]


def test_an_org_name_of_100_characters_is_fine():
    assert Settings.check({**ENV, "PORTAL_ORG_NAME": "a" * 100})[0].org_name == "a" * 100


@pytest.mark.parametrize("value", ["abc", "-1", "1.5", "36501", "30 days"])
def test_bad_grace_days_are_refused(value):
    s, problems = Settings.check({**ENV, "PORTAL_LIFECYCLE_GRACE_DAYS": value})
    assert s is None and set(problems) == {"PORTAL_LIFECYCLE_GRACE_DAYS"}
    assert "never" in problems["PORTAL_LIFECYCLE_GRACE_DAYS"] and "off" not in problems["PORTAL_LIFECYCLE_GRACE_DAYS"]   # 0 is not "no time"


@pytest.mark.parametrize("value", ["real", "Mock", "mock\n", "https://x.service-now.com"])
def test_an_unknown_servicenow_is_refused(value):
    s, problems = Settings.check({**ENV, "PORTAL_SERVICENOW": value})
    assert s is None and set(problems) == {"PORTAL_SERVICENOW"} and value not in problems["PORTAL_SERVICENOW"]


@pytest.mark.parametrize("servicenow", ["", "mock"])
def test_a_servicenow_url_is_refused_while_only_the_mock_exists(servicenow):
    url = "https://example-instance.service-now.com"
    s, problems = Settings.check({**ENV, "PORTAL_SERVICENOW": servicenow, "PORTAL_SERVICENOW_URL": url})
    assert s is None and set(problems) == {"PORTAL_SERVICENOW_URL"} and url not in problems["PORTAL_SERVICENOW_URL"]


def test_the_mock_servicenow_for_the_web_s_task_check_knows_the_org():
    s, problems = Settings.check({**ENV, "PORTAL_SERVICENOW": "mock"})
    assert problems == {} and s.servicenow is True and s.servicenow_api.org == "acme"
    s, problems = Settings.check(ENV)
    assert problems == {} and s.servicenow is False and s.servicenow_api is None
