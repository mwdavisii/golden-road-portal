from portal.web.cookies import SESSION_COOKIE


def test_sign_out_needs_the_session_s_csrf_token(portal):
    client = portal.client("owner")
    assert client.post("/logout").status_code == 403
    assert client.post("/logout", data={"csrf_token": "wrong"}).status_code == 403
    assert client.post("/logout", data={"csrf_token": portal.csrfs["other"]}).status_code == 403  # another session's token
    response = client.post("/logout", data={"csrf_token": portal.csrfs["owner"]}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    response = client.post("/logout", data={"csrf_token": portal.csrfs["owner"]})
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert portal.store.get_session(portal.tokens["owner"], now=portal.clock(), idle=portal.settings.session_idle) is None
    assert any(SESSION_COOKIE in c and "Max-Age=0" in c for c in response.headers.get_list("set-cookie"))


def test_the_csrf_header_works_for_htmx_requests(portal):
    assert portal.client("owner").post("/logout", headers={"X-CSRF-Token": portal.csrfs["owner"]}).status_code == 303


def test_sign_out_without_a_session_is_401(portal):
    assert portal.client().post("/logout").status_code == 401


def test_the_referrer_policy_lets_the_browser_send_the_real_origin(portal):
    """With Referrer-Policy: no-referrer a browser sends `Origin: null` on its own form POSTs (Fetch: serializing a request origin),
    so the Origin check refused every same-origin form. same-origin sends the real origin to us and no referrer to anyone else."""
    assert portal.client().get("/").headers["referrer-policy"] == "same-origin"


def test_a_null_origin_is_still_refused(portal):
    client = portal.client("owner")
    response = client.post("/logout", data={"csrf_token": portal.csrfs["owner"]}, headers={"Origin": "null"})
    assert response.status_code == 403
