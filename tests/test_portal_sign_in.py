from urllib.parse import parse_qs, quote, urlsplit

import pytest

from portal.web.cookies import LOGIN_COOKIE, SESSION_COOKIE


def sign_in(portal, client, *, sub="sub-new", groups=(), next_path=None):
    url = "/login" + (f"?next={next_path}" if next_path else "")
    start = client.get(url)
    assert start.status_code == 302
    location = start.headers["location"]
    code = portal.fakes["idp"].authorize(location, sub=sub, groups=list(groups))
    state = parse_qs(urlsplit(location).query)["state"][0]
    return client.get(f"/auth/callback?code={code}&state={state}")


def test_sign_in_creates_the_person_and_a_hardened_session_cookie(portal):
    client = portal.client()
    done = sign_in(portal, client, next_path="/register")
    assert done.status_code == 303 and done.headers["location"] == "/register"
    cookie = next(c for c in done.headers.get_list("set-cookie") if c.startswith(SESSION_COOKIE + "="))
    for attribute in ("Secure", "HttpOnly", "SameSite=lax", "Path=/"):
        assert attribute.lower() in cookie.lower(), attribute
    assert "domain=" not in cookie.lower()
    assert any(p.subject == "sub-new" for p in portal.store.list_people())
    assert client.get("/healthz").status_code == 200


def test_the_login_redirect_uses_pkce_s256_a_state_and_a_nonce(portal):
    location = portal.client().get("/login").headers["location"]
    q = parse_qs(urlsplit(location).query)
    assert q["code_challenge_method"] == ["S256"] and q["response_type"] == ["code"]
    assert len(q["state"][0]) >= 32 and len(q["nonce"][0]) >= 32 and len(q["code_challenge"][0]) >= 43
    assert q["redirect_uri"] == ["https://portal.example.test/auth/callback"]


def test_the_callback_needs_the_browser_s_login_cookie(portal):
    client = portal.client()
    location = client.get("/login").headers["location"]
    code = portal.fakes["idp"].authorize(location, sub="s", groups=[])
    state = parse_qs(urlsplit(location).query)["state"][0]
    client.cookies.delete(LOGIN_COOKIE)
    assert portal.client().get(f"/auth/callback?code={code}&state={state}").status_code == 400  # login CSRF: another browser


def test_a_login_state_is_single_use_and_expires(portal):
    client = portal.client()
    location = client.get("/login").headers["location"]
    state = parse_qs(urlsplit(location).query)["state"][0]
    portal.clock.advance(minutes=11)
    code = portal.fakes["idp"].authorize(location, sub="s", groups=[])
    assert client.get(f"/auth/callback?code={code}&state={state}").status_code == 400


def test_a_login_state_cannot_be_replayed(portal):
    client = portal.client()
    location = client.get("/login").headers["location"]
    state = parse_qs(urlsplit(location).query)["state"][0]
    code = portal.fakes["idp"].authorize(location, sub="s", groups=[])
    assert client.get(f"/auth/callback?code={code}&state={state}").status_code == 303
    code = portal.fakes["idp"].authorize(location, sub="s", groups=[])
    assert client.get(f"/auth/callback?code={code}&state={state}").status_code == 400


def test_groups_map_to_roles_at_sign_in(portal):
    client = portal.client()
    sign_in(portal, client, sub="sub-boss", groups=["golden-road-admins", "x"])
    person = next(p for p in portal.store.list_people() if p.subject == "sub-boss")
    assert person.last_roles == ("admin",)


@pytest.mark.parametrize("override", [{"aud": "other"}, {"nonce": "other"}, {"iss": "https://evil.example/"}, {"exp": 1}])
def test_a_bad_id_token_signs_nobody_in(portal, override):
    portal.fakes["idp"].override = override
    done = sign_in(portal, portal.client(), sub="sub-bad")
    assert done.status_code == 400 and not any(p.subject == "sub-bad" for p in portal.store.list_people())
    assert not any(c.startswith(SESSION_COOKIE + "=") for c in done.headers.get_list("set-cookie"))


@pytest.mark.parametrize("bad", ["//evil.example", "https://evil.example/", "/\\evil.example", "javascript:alert(1)", "/ok\r\nx", "///evil.example", "/" + "a" * 300])
def test_next_is_only_ever_a_local_path(portal, bad):
    done = sign_in(portal, portal.client(), next_path=quote(bad, safe=""))
    assert done.headers["location"] == "/"


def test_safe_next_keeps_ordinary_local_paths():
    from portal.web.routes_auth import safe_next
    assert safe_next("/register") == "/register" and safe_next("/requests/r_1") == "/requests/r_1" and safe_next("") == "/"
    assert safe_next("//host") == "/" and safe_next("http://host") == "/" and safe_next(None) == "/"


def test_a_discovery_failure_shows_a_page_not_the_provider_s_text(portal):
    portal.fakes["idp"].issuer = "https://sso.example.test/application/o/renamed/"  # discovery now names another issuer
    response = portal.client().get("/login")
    assert response.status_code == 502 and "renamed" not in response.text
