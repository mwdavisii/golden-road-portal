import datetime as dt

import httpx
import jwt
import pytest
from portal_fakes import FakeGitHub, new_rsa_key, pem, transport

from portal.github.appauth import InstallationToken, app_jwt
from portal.github.client import GitHub, GitHubError

API = "https://api.github.com"


def test_the_app_jwt_is_rs256_short_lived_and_names_the_app():
    key = new_rsa_key()
    token = app_jwt(424242, pem(key), now=1_800_000_000)
    claims = jwt.decode(token, key.public_key(), algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False})
    assert claims["iss"] == "424242" and claims["iat"] == 1_800_000_000 - 60 and claims["exp"] - claims["iat"] <= 600


def test_installation_tokens_are_cached_until_near_expiry():
    gh, key = FakeGitHub(), new_rsa_key()
    expires = (dt.datetime(2026, 10, 23, 13, tzinfo=dt.UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    gh.on("POST", "/app/installations/777/access_tokens", (201, {"token": "ghs_" + "a" * 36, "expires_at": expires}))
    now = [dt.datetime(2026, 10, 23, 12, tzinfo=dt.UTC).timestamp()]
    tokens = InstallationToken(httpx.Client(transport=transport(gh)), API, 424242, pem(key), 777, clock=lambda: now[0])
    assert tokens() == tokens() == "ghs_" + "a" * 36 and len(gh.calls) == 1
    bearer = gh.calls[0]["headers"]["authorization"].split()[1]
    assert jwt.decode(bearer, key.public_key(), algorithms=["RS256"], options={"verify_exp": False})["iss"] == "424242"
    now[0] += 56 * 60
    tokens()
    assert len(gh.calls) == 2


def test_pagination_follows_next_on_the_api_host_only():
    gh = FakeGitHub()
    gh.on("GET", "/installation/repositories",
          (200, {"repositories": [{"full_name": "acme/a"}]}, {"link": f'<{API}/installation/repositories?page=2>; rel="next"'}),
          (200, {"repositories": [{"full_name": "acme/b"}]}, {"link": '<https://evil.example/x>; rel="next"'}))
    client = GitHub(API, httpx.Client(transport=transport(gh)), lambda: "ghs_x")
    with pytest.raises(GitHubError, match="leaves the API host"):
        list(client.paginate("/installation/repositories", key="repositories"))
    assert [c["path"] for c in gh.calls] == ["/installation/repositories", "/installation/repositories"]


def test_an_unexpected_status_is_a_github_error_without_github_s_text():
    gh = FakeGitHub().on("GET", "/repos/acme/x", (500, {"message": "<html>boom"}))
    with pytest.raises(GitHubError) as err:
        GitHub(API, httpx.Client(transport=transport(gh)), lambda: "ghs_x").call("GET", "/repos/acme/x")
    assert (err.value.status, err.value.method, err.value.path) == (500, "GET", "/repos/acme/x") and "boom" not in str(err.value)


def test_an_installation_token_keeps_the_permissions_it_was_granted():
    gh, key = FakeGitHub(), new_rsa_key()
    expires = (dt.datetime(2026, 10, 31, 13, tzinfo=dt.UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    gh.on("POST", "/app/installations/777/access_tokens", (201, {"token": "ghs_" + "a" * 36, "expires_at": expires,
                                                                 "permissions": {"metadata": "read", "contents": "read", "actions": "read"}}))
    tokens = InstallationToken(httpx.Client(transport=transport(gh)), API, 424242, pem(key), 777,
                               clock=lambda: dt.datetime(2026, 10, 31, 12, tzinfo=dt.UTC).timestamp())
    assert tokens.permissions == {}
    tokens()
    assert tokens.permissions == {"metadata": "read", "contents": "read", "actions": "read"}
