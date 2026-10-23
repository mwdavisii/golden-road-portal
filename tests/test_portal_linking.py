from urllib.parse import parse_qs, urlsplit

import pytest

from portal.web.cookies import LINK_COOKIE


def start(portal, who="other"):
    client = portal.client(who)
    response = client.get("/link/github")
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize?")
    q = parse_qs(urlsplit(location).query)
    assert q["client_id"] == ["Iv1.testclient"] and q["redirect_uri"] == ["https://portal.example.test/link/github/callback"]
    return client, q["state"][0]


def answer_github(portal, login="other-gh", uid=2002, token="ghu_" + "t" * 36):
    gh = portal.fakes["github"]
    gh.on("POST", "/login/oauth/access_token", (200, {"access_token": token, "token_type": "bearer"}), host="github.com")
    gh.on("GET", "/user", (200, {"login": login, "id": uid}))
    gh.on("DELETE", "/applications/Iv1.testclient/token", (204, None))
    return gh


def test_linking_proves_the_login_and_never_keeps_the_token(portal):
    client, state = start(portal)
    gh = answer_github(portal)
    done = client.get(f"/link/github/callback?code=c-1&state={state}")
    assert done.status_code == 303 and done.headers["location"] == "/me"
    [link] = portal.store.links_for(portal.people["other"].id)
    assert (link.github_login, link.github_user_id, link.source) == ("other-gh", 2002, "github-oauth")
    assert gh.called("DELETE", "/applications/Iv1.testclient/token"), "the user token is revoked"
    assert gh.called("GET", "/user")[0]["headers"]["authorization"] == "Bearer ghu_" + "t" * 36
    everything = repr(portal.store.__dict__)
    assert "ghu_" not in everything, "the user token reached the store or the audit log"
    assert "ghu_" not in repr(portal.store.audit_entries(1000))


def test_another_person_s_state_is_refused(portal):
    _, state = start(portal, "other")
    thief = portal.client("ops")
    thief.cookies.set(LINK_COOKIE, state, domain="portal.example.test")
    answer_github(portal)
    assert thief.get(f"/link/github/callback?code=c&state={state}").status_code == 400
    assert portal.store.links_for(portal.people["ops"].id) == []


def test_a_login_linked_to_someone_else_is_refused(portal):
    client, state = start(portal, "other")
    answer_github(portal, login="owner-gh", uid=1001)  # owner's seeded link
    response = client.get(f"/link/github/callback?code=c&state={state}")
    assert response.status_code == 409 and "ask an admin" in response.text
    assert portal.store.links_for(portal.people["other"].id) == []


@pytest.mark.parametrize("token_answer, user_answer", [
    ((400, {"error": "bad_verification_code"}), (200, {"login": "x", "id": 1})),
    ((200, {"access_token": "ghu_x"}), (401, {"message": "Bad credentials"})),
    ((200, {"access_token": "ghu_x"}), (200, {"login": "x"})),
    ((200, {"access_token": "ghu_x"}), (200, {"login": "-bad login-", "id": 5})),
])
def test_a_bad_github_answer_links_nothing(portal, token_answer, user_answer):
    client, state = start(portal)
    gh = portal.fakes["github"]
    gh.on("POST", "/login/oauth/access_token", token_answer, host="github.com")
    gh.on("GET", "/user", user_answer)
    gh.on("DELETE", "/applications/Iv1.testclient/token", (204, None))
    response = client.get(f"/link/github/callback?code=c&state={state}")
    assert response.status_code == 400 and portal.store.links_for(portal.people["other"].id) == []
    assert "Bad credentials" not in response.text


def test_me_shows_the_person_roles_and_links(portal):
    text = portal.client("owner").get("/me").text
    assert "Owner" in text and "owner-gh" in text and "Link your GitHub login" in text
