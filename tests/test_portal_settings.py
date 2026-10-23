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


@pytest.mark.parametrize("value", ["59", "0", "-5", "soon", "700000"])
def test_a_bad_staleness_limit_stops_startup(value):
    with pytest.raises(SettingsError):
        Settings.from_env({**ENV, "PORTAL_REPO_ADMINS_STALE_AFTER": value})
