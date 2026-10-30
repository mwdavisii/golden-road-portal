"""The mock Secret Server (spec 2026-10-29 Delinea secrets, "The mock"): real response shapes over dummy data, per-user visibility,
the platform account's visibility, 404 for anything outside the client's table (every value endpoint among them), no value in any
answer, a strict loader, and the person mapped through a verified GitHub link."""

import copy
import json
import types

import httpx
import pytest

from portal import secretserver_mock
from portal.secretserver import SecretServerError
from portal.secretserver_mock import MOCK, MockSecretServerError, load_mock, parse

BASE = "https://secretserver.mock/SecretServer"
SHIPPED = json.loads(MOCK.read_text(encoding="utf-8"))


def doc():
    """The shipped mock with mwdavisii's GitHub login replaced by the test owner's (portal_fakes.seed links owner-gh)."""
    d = copy.deepcopy(SHIPPED)
    for user in d["users"]:
        if user["github_login"] == "mwdavisii":
            user["github_login"] = "owner-gh"
    return d


@pytest.fixture
def mock():
    return parse(doc())


@pytest.fixture
def raw(mock):
    """A plain httpx client on the mock's transport, to see exactly what it answers."""
    return httpx.Client(transport=mock.transport(), base_url=BASE)


def link(login, user_id, source="github-oauth"):
    return types.SimpleNamespace(github_login=login, github_user_id=user_id, source=source)


def test_the_shipped_mock_loads():
    m = load_mock()
    assert m.platform_user == "golden-road-eso" and m.user_for([link("mwdavisii", 7)]) == "mwdavisii"


def test_a_person_sees_the_secrets_in_their_folders(mock):
    c = mock.client()
    t = c.token("mwdavisii", "anything")
    assert sorted(s.id for s in c.search_secrets(t)) == [1042, 1043, 1044, 1050]
    assert sorted(s.id for s in mock.client().search_secrets(c.token("other", "x"))) == [2001]


def test_the_platform_sees_the_secrets_shared_with_it(mock):
    c = mock.client()
    t = c.token("golden-road-eso", "x")
    assert sorted(s.id for s in c.search_secrets(t)) == [1042, 1044, 1050, 2001]
    assert c.shared_with_platform(t, 1043) is False and c.shared_with_platform(t, 1042) is True
    assert c.shared_with_platform(t, 9999) is False


def test_search_filters_by_text_and_folder(mock):
    c = mock.client()
    t = c.token("mwdavisii", "x")
    assert [s.name for s in c.search_secrets(t, text="NOTES-D")] == ["notes-db"]
    assert [s.id for s in c.search_secrets(t, folder_id=18)] == [1050]
    assert c.search_secrets(t, folder_id=30) == []
    [s] = c.search_secrets(t, text="notes-db")
    assert (s.folder_id, s.template_id, s.template_name) == (17, 6001, "Password")


def test_search_pages(mock):
    c = mock.client()
    c.PAGE_SIZE = 1
    assert sorted(s.id for s in c.search_secrets(c.token("mwdavisii", "x"))) == [1042, 1043, 1044, 1050]
    page = mock.client().http.get(f"{BASE}/api/v1/secrets", params={"skip": 1, "take": 2},
                                  headers={"Authorization": "Bearer mock-mwdavisii"}).json()
    assert (page["total"], page["skip"], page["take"], page["hasNext"]) == (4, 1, 2, True)
    assert set(page["records"][0]) == {"id", "name", "folderId", "secretTemplateId", "secretTemplateName", "active"}


@pytest.mark.parametrize("folder", ["\u0661\u0667", "\u00b9\u2077", "17 ", "-17", "1.7", ""])
def test_the_folder_filter_takes_ascii_digits_only(raw, folder):
    r = raw.get("/api/v1/secrets", params={"filter.folderId": folder}, headers={"Authorization": "Bearer mock-mwdavisii"})
    assert r.status_code == 400


def test_folders_are_the_user_s(mock):
    c = mock.client()
    assert c.folders(c.token("mwdavisii", "x")) == {17: "\\Golden Road\\hello-app", 18: "\\Golden Road\\golden-road-portal"}
    assert c.folders(c.token("mwdavisii", "x"), text="portal") == {18: "\\Golden Road\\golden-road-portal"}
    assert c.folders(c.token("golden-road-eso", "x")) == {}


def test_template_fields_never_offer_a_file_field(mock):
    c = mock.client()
    t = c.token("mwdavisii", "x")
    assert [f.slug for f in c.template_fields(t, 6010)] == ["passphrase"]
    assert [f.slug for f in c.template_fields(t, 6001)] == ["username", "password", "url", "notes"]
    with pytest.raises(SecretServerError):
        c.template_fields(t, 9999)


def test_the_summary_is_the_user_s_visibility(raw):
    auth = {"Authorization": "Bearer mock-mwdavisii"}
    r = raw.get("/api/v1/secrets/1042/summary", headers=auth)
    assert r.status_code == 200 and r.json()["id"] == 1042 and r.json()["name"] == "notes-db"
    assert raw.get("/api/v1/secrets/2001/summary", headers=auth).status_code == 403


@pytest.mark.parametrize("method, path", [
    ("GET", "/api/v1/secrets/1042"), ("GET", "/api/v1/secrets/1042/fields/password"), ("PUT", "/api/v1/secrets/1042/fields/password"),
    ("GET", "/api/v2/secrets/1042"), ("GET", "/api/v1/secrets/1042/restricted"), ("POST", "/api/v1/secrets/1042/restricted"),
    ("GET", "/api/v1/secrets/1042/fields/password/listdetails"), ("POST", "/api/v1/secrets"), ("DELETE", "/api/v1/secrets/1042"),
    ("GET", "/api/v1/folders/17"), ("GET", "/api/v1/users"), ("GET", "/oauth2/token"), ("GET", "/api/v1/secret-templates")])
def test_anything_outside_the_table_is_404(raw, method, path):
    r = raw.request(method, path, headers={"Authorization": "Bearer mock-mwdavisii"})
    assert r.status_code == 404 and r.json() == {"message": "not part of the mock"}


def test_a_missing_or_unknown_bearer_is_401(raw):
    assert raw.get("/api/v1/secrets").status_code == 401
    assert raw.get("/api/v1/secrets", headers={"Authorization": "Bearer mock-nobody"}).status_code == 401
    assert raw.get("/api/v1/folders", headers={"Authorization": "Basic bW9jay1td2RhdmlzaWk="}).status_code == 401
    assert raw.get("/api/v1/secret-templates/6001", headers={"Authorization": "Bearer mwdavisii"}).status_code == 401


def test_the_token_is_the_password_grant_for_a_mock_user(raw):
    r = raw.post("/oauth2/token", data={"grant_type": "password", "username": "mwdavisii", "password": "x"})
    assert r.json() == {"access_token": "mock-mwdavisii", "token_type": "bearer", "expires_in": 1200}
    assert raw.post("/oauth2/token", data={"grant_type": "password", "username": "nobody", "password": "x"}).status_code == 400
    assert raw.post("/oauth2/token", data={"grant_type": "client_credentials", "username": "mwdavisii"}).status_code == 400
    assert raw.post("/oauth2/token", json={"grant_type": "password", "username": "mwdavisii"}).status_code == 400


def _walk(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v)


def test_no_answer_carries_a_value(raw):
    seen = []
    for who in ("mwdavisii", "other", "golden-road-eso"):
        auth = {"Authorization": f"Bearer mock-{who}"}
        seen += [raw.get("/api/v1/secrets", params={"take": 100}, headers=auth), raw.get("/api/v1/folders", headers=auth)]
        seen += [raw.get(f"/api/v1/secret-templates/{t}", headers=auth) for t in (6001, 6003, 6010)]
        seen += [raw.get(f"/api/v1/secrets/{s}/summary", headers=auth) for s in (1042, 1043, 1044, 1050, 2001)]
        seen += [raw.get(f"/api/v1/secrets/{s}", headers=auth) for s in (1042, 2001)]
    for r in seen:
        keys = set(_walk(r.json()))
        assert not keys & {"items", "itemValue", "value", "password"}, r.request.url
        assert secretserver_mock.MOCK_VALUE not in r.text


def test_user_for_never_takes_an_admin_made_link(mock):
    """An admin-made link is not the person's own proof, even when the collector has filled in its id."""
    assert mock.user_for([link("owner-gh", 1001, source="admin")]) is None
    assert mock.user_for([link("owner-gh", None, source="admin")]) is None
    assert mock.user_for([link("owner-gh", 1001, source="admin"), link("other", 5)]) == "other"
    assert mock.user_for([types.SimpleNamespace(github_login="owner-gh", github_user_id=1001)]) is None  # no source: never


def test_user_for_takes_a_store_link_the_person_verified(mock):
    import datetime as dt
    from portal.store.base import Link
    now = dt.datetime(2026, 10, 30, tzinfo=dt.UTC)
    own = Link("l1", "p1", "owner-gh", 1001, "github-oauth", "p1", now)
    made = Link("l2", "p1", "owner-gh", 1001, "admin", "p2", now)
    assert mock.user_for([own]) == "mwdavisii" and mock.user_for([made]) is None


def test_user_for_takes_only_a_verified_link(mock):
    assert mock.user_for([link("owner-gh", 1001)]) == "mwdavisii"
    assert mock.user_for([link("OWNER-GH", 1001)]) == "mwdavisii"
    assert mock.user_for([link("owner-gh", None)]) is None  # a link an admin made by name only, never verified through OAuth
    assert mock.user_for([link("owner-gh", None), link("other", 5)]) == "other"
    assert mock.user_for([]) is None and mock.user_for([link("stranger", 9)]) is None


def test_the_platform_account_is_never_a_person(mock):
    assert mock.user_for([link("golden-road-eso", 1)]) is None and mock.user_for([link(None, 1)]) is None


def _bad(change):
    d = doc()
    change(d)
    return d


BAD = {
    "unknown top key": lambda d: d.update(extra=1),
    "missing platform_user": lambda d: d.pop("platform_user"),
    "platform_user not a user": lambda d: d.update(platform_user="ghost"),
    "wrong schema": lambda d: d.update(schema="mock-secret-server/v2"),
    "unknown folder key": lambda d: d["folders"][0].update(owner="x"),
    "unknown secret key": lambda d: d["secrets"][0].update(items=[{"itemValue": "<mock>"}]),
    "unknown field key": lambda d: d["templates"][0]["fields"][0].update(itemValue="x"),
    "unknown user key": lambda d: d["users"][0].update(password="x"),
    "duplicate folder id": lambda d: d["folders"][1].update(id=17),
    "duplicate secret id": lambda d: d["secrets"][1].update(id=1042),
    "duplicate template id": lambda d: d["templates"][1].update(id=6001),
    "duplicate username": lambda d: d["users"][1].update(username="mwdavisii"),
    "duplicate field slug": lambda d: d["templates"][0]["fields"][1].update(fieldSlugName="username"),
    "id zero": lambda d: d["secrets"][0].update(id=0),
    "id negative": lambda d: d["folders"][0].update(id=-1),
    "id a bool": lambda d: d["secrets"][0].update(id=True),
    "id a string": lambda d: d["secrets"][0].update(id="1042"),
    "missing folder": lambda d: d["secrets"][0].update(folderId=99),
    "missing template": lambda d: d["secrets"][0].update(secretTemplateId=99),
    "shared with a stranger": lambda d: d["secrets"][0].update(shared_with=["ghost"]),
    "unknown user folder": lambda d: d["users"][0].update(folders=[17, 99]),
    "long name": lambda d: d["secrets"][0].update(name="x" * 201),
    "empty name": lambda d: d["secrets"][0].update(name=""),
    "control character": lambda d: d["secrets"][0].update(name="notes\x07db"),
    "format character": lambda d: d["folders"][0].update(folderPath="\\Golden\u202eRoad"),
    "bidi in a template name": lambda d: d["templates"][0].update(name="Pass\u200bword"),
    "isFile not a bool": lambda d: d["templates"][0]["fields"][0].update(isFile="no"),
    "bad slug": lambda d: d["templates"][0]["fields"][0].update(fieldSlugName="User Name"),
    "bad username": lambda d: d["users"][0].update(username="mw davis"),
    "folders not a list": lambda d: d.update(folders={}),
    "github_login not text": lambda d: d["users"][0].update(github_login=7),
    "platform user with a github_login": lambda d: d["users"][2].update(github_login="golden-road-eso"),
    "two users with one github_login": lambda d: d["users"][1].update(github_login="OWNER-GH"),
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_a_bad_mock_file_is_refused(name):
    with pytest.raises(MockSecretServerError):
        parse(_bad(BAD[name]))


def test_load_mock_refuses_a_duplicate_key_a_broken_file_and_a_missing_one(tmp_path, monkeypatch):
    path = tmp_path / "mock.json"
    path.write_text('{"schema": "mock-secret-server/v1", "schema": "mock-secret-server/v1"}')
    monkeypatch.setattr(secretserver_mock, "MOCK", path)
    with pytest.raises(MockSecretServerError):
        load_mock()
    path.write_text("{")
    with pytest.raises(MockSecretServerError):
        load_mock()
    monkeypatch.setattr(secretserver_mock, "MOCK", tmp_path / "missing.json")
    with pytest.raises(MockSecretServerError):
        load_mock()


def test_a_person_s_summary_is_what_they_can_see(mock):
    with mock.client() as c:
        t = c.token("mwdavisii", "x")
        s = c.secret_summary(t, 1043)   # in their folder, though not shared with the platform
        assert (s.id, s.name, s.folder_id, s.template_id) == (1043, "notes-api-key", 17, 6003)
        assert c.secret_summary(t, 2001) is None and c.secret_summary(t, 9999) is None
        found, more = c.search_first(t, take=2)
        assert len(found) == 2 and more is True
