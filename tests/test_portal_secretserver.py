"""The Secret Server client (spec 2026-10-29 Delinea secrets, "The real API the client uses"): each call is exactly the request in
the spec's table, paging works, errors are clear and never mean "shared", and no request ever asks for a value."""

import re

import httpx
import pytest

from portal.secretserver import Client, SecretServerError

BASE = "https://ss.example.test/SecretServer"
# GET /api/v1/secrets/{id} returns items[].itemValue and /fields/{slug} returns a value: the client must never ask for either.
VALUE_PATH = re.compile(r"/api/v1/secrets/\d+/?$|/fields(/|$)")


def transport(routes, seen):
    def handle(request):
        seen.append((request.method, request.url.path, dict(request.url.params), request.headers.get("authorization")))
        if VALUE_PATH.search(request.url.path):
            pytest.fail(f"the client asked for a value: {request.method} {request.url.path}")
        key = (request.method, request.url.path)
        if key not in routes:
            return httpx.Response(404, json={"message": "not in the test"})
        status, body = routes[key](request) if callable(routes[key]) else routes[key]
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handle)


def client(routes, seen):
    return Client(BASE, http=httpx.Client(transport=transport(routes, seen)))


EMPTY = {"total": 0, "skip": 0, "take": 50, "hasNext": False, "records": []}


def test_token_is_the_password_grant_and_bearer_is_used_after():
    seen, forms = [], []

    def token(request):
        forms.append((request.headers["content-type"], request.content.decode()))
        return 200, {"access_token": "t1", "token_type": "bearer", "expires_in": 1200}
    routes = {("POST", "/SecretServer/oauth2/token"): token, ("GET", "/SecretServer/api/v1/secrets"): (200, EMPTY)}
    c = client(routes, seen)
    assert c.token("mwdavisii", "pw") == "t1"
    assert forms == [("application/x-www-form-urlencoded", "grant_type=password&username=mwdavisii&password=pw")]
    assert c.search_secrets("t1", text="notes") == []
    method, path, params, auth = seen[1]
    assert (method, path, auth) == ("GET", "/SecretServer/api/v1/secrets", "Bearer t1")
    assert params == {"filter.searchText": "notes", "filter.includeRestricted": "true", "skip": "0", "take": "50"}


def test_search_by_folder_sends_the_folder_filter():
    seen = []
    c = client({("GET", "/SecretServer/api/v1/secrets"): (200, EMPTY)}, seen)
    c.search_secrets("t", text="", folder_id=17)
    assert seen[0][2]["filter.folderId"] == "17" and seen[0][2]["filter.searchText"] == ""


def test_search_parses_the_real_record_shape():
    rec = {"id": 1042, "name": "notes-db", "folderId": 17, "secretTemplateId": 6001, "secretTemplateName": "Password", "active": True}
    c = client({("GET", "/SecretServer/api/v1/secrets"): (200, {**EMPTY, "total": 1, "records": [rec]})}, [])
    [s] = c.search_secrets("t")
    assert (s.id, s.name, s.folder_id, s.template_id, s.template_name) == (1042, "notes-db", 17, 6001, "Password")


def test_search_pages_with_skip_and_take():
    pages = {0: [{"id": 1, "name": "a", "folderId": 1, "secretTemplateId": 6001, "secretTemplateName": "Password"}],
             1: [{"id": 2, "name": "b", "folderId": 1, "secretTemplateId": 6001, "secretTemplateName": "Password"}]}

    def page(request):
        skip = int(request.url.params["skip"])
        assert request.url.params["take"] == "1"
        return 200, {"total": 2, "skip": skip, "take": 1, "hasNext": skip == 0, "records": pages[skip]}
    c = client({("GET", "/SecretServer/api/v1/secrets"): page}, [])
    c.PAGE_SIZE = 1
    assert [s.id for s in c.search_secrets("t", text="")] == [1, 2]


def test_a_search_that_never_ends_is_refused():
    def page(request):
        return 200, {"total": 10**6, "skip": 0, "take": 1, "hasNext": True,
                     "records": [{"id": 1, "name": "a", "folderId": 1, "secretTemplateId": 1}]}
    c = client({("GET", "/SecretServer/api/v1/secrets"): page}, [])
    c.PAGE_SIZE, c.MAX_PAGES = 1, 3
    with pytest.raises(SecretServerError, match="narrow the search"):
        c.search_secrets("t")


def test_folders_are_named_by_path():
    seen = []
    body = {**EMPTY, "records": [{"id": 17, "folderName": "hello-app", "folderPath": "\\Golden Road\\hello-app"}]}
    c = client({("GET", "/SecretServer/api/v1/folders"): (200, body)}, seen)
    assert c.folders("t", text="Golden") == {17: "\\Golden Road\\hello-app"}
    assert seen[0][2]["filter.searchText"] == "Golden" and seen[0][3] == "Bearer t"


def test_template_fields_drop_file_fields():
    body = {"id": 6001, "name": "Password", "fields": [
        {"fieldSlugName": "username", "displayName": "Username", "isPassword": False, "isFile": False},
        {"fieldSlugName": "password", "displayName": "Password", "isPassword": True, "isFile": False},
        {"fieldSlugName": "key-file", "displayName": "Key file", "isPassword": False, "isFile": True}]}
    c = client({("GET", "/SecretServer/api/v1/secret-templates/6001"): (200, body)}, [])
    fields = c.template_fields("t", 6001)
    assert [f.slug for f in fields] == ["username", "password"] and fields[1].is_password and fields[0].display_name == "Username"


@pytest.mark.parametrize("status, shared", [(200, True), (403, False), (404, False)])
def test_shared_with_platform_is_the_summary_as_the_platform(status, shared):
    seen = []
    c = client({("GET", "/SecretServer/api/v1/secrets/1042/summary"): (status, {"id": 1042, "name": "notes-db", "folderId": 17,
                                                                                 "secretTemplateId": 6001})}, seen)
    assert c.shared_with_platform("platform-token", 1042) is shared
    assert seen[0][3] == "Bearer platform-token"


@pytest.mark.parametrize("status", [401, 500, 502, 503])
def test_errors_are_clear_and_never_mean_shared(status):
    c = client({("GET", "/SecretServer/api/v1/secrets/1042/summary"): (status, {})}, [])
    with pytest.raises(SecretServerError, match=str(status)):
        c.shared_with_platform("t", 1042)


@pytest.mark.parametrize("status, words", [(401, "token"), (403, "not allowed"), (404, "not found"), (500, "failed"),
                                           (502, "failed")])
def test_search_errors_map_to_clear_messages(status, words):
    c = client({("GET", "/SecretServer/api/v1/secrets"): (status, {"message": "boom"})}, [])
    with pytest.raises(SecretServerError, match=words):
        c.search_secrets("t")


def test_a_summary_that_is_not_the_secret_asked_for_is_an_error():
    c = client({("GET", "/SecretServer/api/v1/secrets/1042/summary"): (200, {"id": 9})}, [])
    with pytest.raises(SecretServerError):
        c.shared_with_platform("t", 1042)


@pytest.mark.parametrize("status, body", [(400, {"error": "invalid_grant"}), (200, {"token_type": "bearer"}), (500, {})])
def test_a_refused_token_never_names_the_credentials(status, body):
    c = client({("POST", "/SecretServer/oauth2/token"): (status, body)}, [])
    with pytest.raises(SecretServerError) as caught:
        c.token("golden-road-eso", "hunter2-password")
    assert "hunter2" not in str(caught.value) and "golden-road-eso" not in str(caught.value)


def test_an_unreachable_server_names_neither_token_nor_url_credentials():
    def boom(request):
        raise httpx.ConnectError(f"failed for {request.headers.get('authorization')}")
    c = Client(BASE, http=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(SecretServerError, match="could not be reached") as caught:
        c.search_secrets("secret-token-value")
    assert "secret-token-value" not in str(caught.value) and caught.value.__cause__ is None and caught.value.__context__ is None
    with pytest.raises(SecretServerError) as caught:
        c.token("u", "hunter2-password")
    assert "hunter2" not in str(caught.value) and caught.value.__cause__ is None and caught.value.__context__ is None


def test_every_request_has_a_timeout():
    timeouts = []

    def handle(request):
        timeouts.append(request.extensions.get("timeout"))
        return httpx.Response(200, json={"access_token": "t"} if request.method == "POST" else EMPTY)
    c = Client(BASE, http=httpx.Client(transport=httpx.MockTransport(handle), timeout=None))
    c.token("u", "p")
    c.search_secrets("t")
    assert timeouts and all(t and all(v is not None for v in t.values()) for t in timeouts)


def test_the_client_never_requests_a_value():
    seen = []
    routes = {("POST", "/SecretServer/oauth2/token"): (200, {"access_token": "t", "token_type": "bearer", "expires_in": 1}),
              ("GET", "/SecretServer/api/v1/secrets"): (200, {"total": 1, "skip": 0, "take": 50, "hasNext": False, "records": [
                  {"id": 1042, "name": "notes-db", "folderId": 17, "secretTemplateId": 6001, "secretTemplateName": "Password"}]}),
              ("GET", "/SecretServer/api/v1/folders"): (200, EMPTY),
              ("GET", "/SecretServer/api/v1/secret-templates/6001"): (200, {"id": 6001, "fields": []}),
              ("GET", "/SecretServer/api/v1/secrets/1042/summary"): (200, {"id": 1042})}
    c = client(routes, seen)
    t = c.token("u", "p")
    c.search_secrets(t, text="")
    c.folders(t)
    c.template_fields(t, 6001)
    c.shared_with_platform(t, 1042)
    assert len(seen) == 5 and not [path for _, path, _, _ in seen if VALUE_PATH.search(path)]
    assert not any(name.startswith(("value", "secret_value", "field_value", "get_secret")) for name in dir(c))


def test_the_value_guard_itself_fails_a_value_request():
    c = client({}, [])
    with pytest.raises(BaseException, match="asked for a value"):
        c._get("t", "/api/v1/secrets/1042")
    with pytest.raises(BaseException, match="asked for a value"):
        c._get("t", "/api/v1/secrets/1042/fields/password")


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_an_error_and_never_followed(status):
    """A redirect from /summary to the value endpoint must not be fetched: the client never follows one."""
    seen = []

    def handle(request):
        seen.append(request.url.path)
        if VALUE_PATH.search(request.url.path):
            pytest.fail("the client followed a redirect to a value endpoint")
        return httpx.Response(status, headers={"Location": f"{BASE}/api/v1/secrets/1042"})
    for follow in (False, True):  # even an httpx.Client that would follow redirects by default
        c = Client(BASE, http=httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=follow))
        with pytest.raises(SecretServerError, match="redirect"):
            c.shared_with_platform("t", 1042)
        with pytest.raises(SecretServerError, match="redirect"):
            c.search_secrets("t")
        with pytest.raises(SecretServerError, match="redirect"):
            c.token("u", "p")
    assert seen and all(not VALUE_PATH.search(p) for p in seen)


def test_an_answer_for_another_path_is_refused():
    """httpx itself pins a response to its request; a client object that did not (or a future change that followed) is refused."""
    class Elsewhere:
        def request(self, method, url, **kwargs):
            assert kwargs["follow_redirects"] is False and kwargs["timeout"]
            return httpx.Response(200, json={"id": 1042}, request=httpx.Request("GET", f"{BASE}/api/v1/secrets/1042"))
    c = Client(BASE, http=Elsewhere())
    with pytest.raises(SecretServerError, match="another path"):
        c.shared_with_platform("t", 1042)


@pytest.mark.parametrize("path, call", [("/SecretServer/api/v1/secrets", lambda c: c.search_secrets("t")),
                                        ("/SecretServer/api/v1/folders", lambda c: c.folders("t"))])
@pytest.mark.parametrize("record", [None, 7, "a", ["id", 1]])
def test_a_record_that_is_not_an_object_is_an_error(path, call, record):
    c = client({("GET", path): (200, {**EMPTY, "total": 1, "records": [record]})}, [])
    with pytest.raises(SecretServerError, match="not an object"):
        call(c)


@pytest.mark.parametrize("has_next", ["true", 1, "yes", None])
def test_only_has_next_true_asks_for_another_page(has_next):
    seen = []
    rec = {"id": 1, "name": "a", "folderId": 1, "secretTemplateId": 1}
    c = client({("GET", "/SecretServer/api/v1/secrets"): (200, {**EMPTY, "hasNext": has_next, "records": [rec]})}, seen)
    assert [s.id for s in c.search_secrets("t")] == [1] and len(seen) == 1


@pytest.mark.parametrize("display", [None, 7, "", ["Password"], {"text": "x"}])
def test_a_display_name_that_is_not_text_falls_back_to_the_slug(display):
    body = {"id": 6001, "fields": [{"fieldSlugName": "password", "displayName": display, "isPassword": True, "isFile": False}]}
    c = client({("GET", "/SecretServer/api/v1/secret-templates/6001"): (200, body)}, [])
    assert c.template_fields("t", 6001)[0].display_name == "password"


def test_a_folder_name_that_is_not_text_falls_back():
    body = {**EMPTY, "records": [{"id": 17, "folderPath": 5, "folderName": "hello-app"}, {"id": 18, "folderPath": None}]}
    c = client({("GET", "/SecretServer/api/v1/folders"): (200, body)}, [])
    assert c.folders("t") == {17: "hello-app", 18: "18"}


SUMMARY = {"id": 1042, "name": "notes-db", "folderId": 17, "secretTemplateId": 6001, "secretTemplateName": "Password", "active": True}


@pytest.mark.parametrize("status", [403, 404])
def test_secret_summary_is_the_caller_s_view_and_none_when_they_can_t_see_it(status):
    seen = []
    c = client({("GET", "/SecretServer/api/v1/secrets/1042/summary"): (200, SUMMARY)}, seen)
    s = c.secret_summary("person-token", 1042)
    assert (s.id, s.name, s.folder_id, s.template_id, s.template_name) == (1042, "notes-db", 17, 6001, "Password")
    assert seen == [("GET", "/SecretServer/api/v1/secrets/1042/summary", {}, "Bearer person-token")]
    c = client({("GET", "/SecretServer/api/v1/secrets/1042/summary"): (status, {"message": "Access Denied"})}, [])
    assert c.secret_summary("t", 1042) is None


@pytest.mark.parametrize("answer", [(500, {}), (401, {}), (200, {"id": 9}), (200, {"id": 1042}), (200, [])])
def test_a_bad_summary_is_an_error_never_can_see(answer):
    c = client({("GET", "/SecretServer/api/v1/secrets/1042/summary"): answer}, [])
    with pytest.raises(SecretServerError):
        c.secret_summary("t", 1042)


def test_search_first_asks_for_one_page():
    seen = []
    recs = [{"id": i, "name": f"s{i}", "folderId": 1, "secretTemplateId": 1} for i in range(1, 4)]
    c = client({("GET", "/SecretServer/api/v1/secrets"): (200, {**EMPTY, "total": 9, "hasNext": True, "records": recs})}, seen)
    found, more = c.search_first("t", text="s", folder_id=1, take=3)
    assert [s.id for s in found] == [1, 2, 3] and more is True and len(seen) == 1
    assert seen[0][2] == {"filter.searchText": "s", "filter.includeRestricted": "true", "filter.folderId": "1", "skip": "0", "take": "3"}
    c = client({("GET", "/SecretServer/api/v1/secrets"): (200, {**EMPTY, "records": recs[:2]})}, [])
    assert [s.id for s in c.search_first("t", take=3)[0]] == [1, 2] and c.search_first("t", take=3)[1] is False
    c = client({("GET", "/SecretServer/api/v1/secrets"): (200, {**EMPTY, "records": recs})}, [])   # more than asked for
    found, more = c.search_first("t", take=2)
    assert [s.id for s in found] == [1, 2] and more is True


def test_the_client_closes_its_http_client():
    http = httpx.Client(transport=transport({}, []))
    with Client(BASE, http=http) as c:
        assert c.http is http and not http.is_closed
    assert http.is_closed
