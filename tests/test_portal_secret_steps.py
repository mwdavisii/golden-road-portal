"""The secrets picker in steps (spec 2026-10-30 R7): Source (the folders the person can see), Secret (search in the folder, the
unavailable greyed with the reason), Field and name, Review and save. Each step is a GET carrying its choices; every step checks the
earlier ones again; only the final POST creates the binding request."""

import html
import json
import re

import pytest
from test_portal_apps import MOCK, bindings, parse_mock, post, search_calls, with_secret_server

NEW = "/apps/acme/notes/secrets/new"


@pytest.fixture
def secrets(portal):
    return with_secret_server(portal)


def get(portal, who="owner", **params):
    return portal.client(who).get(NEW, params=params)


def radios(text, name):
    return set(re.findall(rf'<input type="radio" name="{name}" value="([^"]+)"', text))


def step_of(text):
    return re.search(r'aria-current="step"><span class="step-dot" aria-hidden="true">(\d)</span>', text).group(1)


def test_step_1_offers_only_the_folders_the_person_can_see(secrets):
    response = get(secrets)
    assert response.status_code == 200 and step_of(response.text) == "1" and radios(response.text, "folder") == {"17", "18"}  # not 30
    assert "\\Golden Road\\hello-app" in html.unescape(response.text) and "golden-road-eso" in response.text


def test_step_2_lists_the_folder_s_secrets_and_greys_the_unavailable_with_the_reason(secrets):
    text = get(secrets, folder="17").text
    assert step_of(text) == "2" and radios(text, "secret_id") == {"1042", "1044"}   # 1043 is not shared with golden-road-eso
    assert "notes-api-key" in text and text.count("Ask security to share it with golden-road-eso.") == 1
    assert '<div class="choice unavailable">' in text and "Not available" in text
    assert radios(get(secrets, folder="18").text, "secret_id") == {"1050"}


def test_step_2_searches_in_the_folder(secrets):
    assert radios(get(secrets, folder="17", q="tls").text, "secret_id") == {"1044"}
    assert "No secret you can see matches." in get(secrets, folder="17", q="nothing-like-this").text


@pytest.mark.parametrize("params, message", [
    ({"folder": "30"}, "Choose a folder from the list."),            # another user's folder
    ({"folder": "１８"}, "Choose a folder from the list."),           # not ASCII digits
    ({"folder": "017"}, "Choose a folder from the list."),
    ({"secret_id": "1042"}, "Start from the source: choose a folder first."),
    ({"field_slug": "password", "env": "X"}, "Choose a secret first."),
    ({"folder": "17", "q": "x" * 101}, "Search text is at most 100 characters."),
])
def test_a_skipped_or_tampered_earlier_choice_is_refused(secrets, params, message):
    response = get(secrets, **params)
    assert response.status_code == 400 and message in response.text and step_of(response.text) == "1"
    assert not bindings(secrets)


def test_secret_server_is_searched_only_at_step_2_and_one_page_of_20(portal, monkeypatch):
    doc = json.loads(MOCK.read_text())
    doc["secrets"] += [{"id": 3000 + i, "name": f"bulk-{i:02d}", "folderId": 17, "secretTemplateId": 6001, "shared_with": []}
                       for i in range(30)]
    with_secret_server(portal, parse_mock(doc))
    calls = search_calls(monkeypatch)
    get(portal)
    assert calls == []   # step 1 lists folders; it never searches
    text = get(portal, folder="17", q="bulk").text
    assert calls == [{"text": "bulk", "folder_id": 17, "take": 20}] and text.count("Ask security to share it with") == 20
    assert "Only the first 20 are shown: narrow the search." in text
    assert "Only the first" not in get(portal, folder="17", q="notes").text


def test_a_secret_server_failure_is_a_plain_note(secrets, monkeypatch):
    from portal.secretserver import Client, SecretServerError

    def fail(self, *a, **kw):
        raise SecretServerError("Secret Server failed (500) answering for /api/v1/folders")
    monkeypatch.setattr(Client, "folders", fail)
    response = get(secrets)
    assert response.status_code == 503 and "Secret Server could not answer: try again." in response.text and "(500)" not in response.text


def test_without_a_verified_link_the_steps_say_why(portal):
    with_secret_server(portal, link=False)
    text = get(portal).text
    assert "Link your GitHub account (verified) first" in html.unescape(text) and radios(text, "folder") == set()


def test_who_may_use_the_steps(secrets):
    assert get(secrets, who="admin").status_code == 403   # a portal admin who isn't the repo's admin
    assert get(secrets, who="other").status_code == 404
    assert secrets.client("owner").get(NEW, params={"environment": "staging"}).status_code == 400


def test_without_secret_server_the_steps_are_404(portal):
    assert get(portal).status_code == 404


def test_while_a_change_is_pending_the_steps_are_409(secrets):
    from test_portal_apps import link_identity_and_merge
    link_identity_and_merge(secrets)
    assert post(secrets, "owner", "/apps/acme/notes/databricks", warehouse_id="wh-notes").status_code == 303
    assert get(secrets).status_code == 409


def test_a_hostile_secret_name_is_escaped(portal):
    doc = json.loads(MOCK.read_text())
    doc["secrets"][0]["name"] = "<script>alert(1)</script>"
    with_secret_server(portal, parse_mock(doc))
    text = get(portal, folder="17").text
    assert "<script>alert(1)</script>" not in text and "&lt;script&gt;alert(1)&lt;/script&gt;" in text


def test_the_steps_pages_are_free_of_inline_style_and_script(secrets):
    from test_portal_csp_pages import problems
    for params in ({}, {"folder": "17"}, {"folder": "17", "secret_id": "1042"},
                   {"folder": "17", "secret_id": "1042", "field_slug": "password", "env": "NOTES_DB_PASSWORD"}):
        assert problems(get(secrets, **params).text) == [], params


def test_step_3_offers_the_secret_s_non_file_fields_and_the_name_rules(secrets):
    text = get(secrets, folder="17", secret_id="1044").text
    assert step_of(text) == "3" and radios(text, "field_slug") == {"passphrase"}   # its certificate is a file field
    assert 'pattern="[A-Z_][A-Z0-9_]*"' in text and 'maxlength="128"' in text and "Using <span class=\"mono\">notes-tls</span>" in text


@pytest.mark.parametrize("params, message, step", [
    ({"folder": "17", "secret_id": "2001"}, "not one you can see in this folder", "2"),
    ({"folder": "18", "secret_id": "1042"}, "not one you can see in this folder", "2"),   # a real secret, from another folder
    ({"folder": "17", "secret_id": "1043"}, "isn't shared with golden-road-eso", "2"),
    ({"folder": "17", "secret_id": "1044", "field_slug": "certificate", "env": "TLS_CERT"}, "not a field this secret can deliver", "3"),
    ({"folder": "17", "secret_id": "1042", "field_slug": "password", "env": "PORT"}, "set by the platform", "3"),
    ({"folder": "17", "secret_id": "1042", "field_slug": "password", "env": "lower"}, "Name the variable", "3"),
    ({"folder": "17", "secret_id": "1042", "field_slug": "", "env": "NOTES_DB_PASSWORD"}, "Choose a secret and one of its fields", "3"),
])
def test_steps_3_and_4_check_every_earlier_choice_again(secrets, params, message, step):
    response = get(secrets, **params)
    assert response.status_code == 400 and message in html.unescape(response.text) and step_of(response.text) == step
    assert not bindings(secrets)


def test_a_refused_name_keeps_what_was_typed(secrets):
    text = get(secrets, folder="17", secret_id="1042", field_slug="password", env="PORT").text
    assert 'name="env" value="PORT"' in text and 'value="password" required checked>' in text


def test_step_4_reviews_without_creating_anything_and_only_the_post_creates_the_request(secrets):
    text = get(secrets, folder="17", secret_id="1042", field_slug="password", env="NOTES_DB_PASSWORD").text
    assert step_of(text) == "4" and "notes-db / password" in text and not bindings(secrets)
    for name, value in (("folder", "17"), ("secret_id", "1042"), ("field_slug", "password"), ("env", "NOTES_DB_PASSWORD")):
        assert f'<input type="hidden" name="{name}" value="{value}">' in text
    response = post(secrets, "owner", "/apps/acme/notes/secrets", folder="17", secret_id="1042", field_slug="password", env="NOTES_DB_PASSWORD")
    assert response.status_code == 303 and len(bindings(secrets)) == 1


@pytest.mark.parametrize("folder", ["", "18", "abc", "１７"])
def test_the_final_post_needs_the_secret_s_own_folder(secrets, folder):
    response = post(secrets, "owner", "/apps/acme/notes/secrets", folder=folder, secret_id="1042", field_slug="password", env="X_DB")
    assert response.status_code == 400 and "Choose the secret from its folder" in response.text and not bindings(secrets)


def test_the_app_page_links_to_the_steps_and_no_longer_searches(secrets, monkeypatch):
    calls = search_calls(monkeypatch)
    page = secrets.client("owner").get("/apps/acme/notes", params={"q": "notes", "folder": "17"}).text
    assert calls == [] and '<a class="button" href="/apps/acme/notes/secrets/new?environment=test">Add a secret</a>' in page
    assert 'name="q"' not in page and 'name="secret_id"' not in page


def test_the_final_post_needs_a_folder_the_person_can_still_see(secrets, monkeypatch):
    from portal.secretserver import Client
    original = Client.folders
    monkeypatch.setattr(Client, "folders", lambda self, token, **kw: {k: v for k, v in original(self, token, **kw).items() if k != 17})
    response = post(secrets, "owner", "/apps/acme/notes/secrets", folder="17", secret_id="1042", field_slug="password", env="X_DB")
    assert response.status_code == 400 and "Choose the secret from its folder" in response.text and not bindings(secrets)


def test_sharing_revoked_between_the_review_and_the_post_is_refused(secrets, monkeypatch):
    from portal.secretserver import Client
    assert step_of(get(secrets, folder="17", secret_id="1042", field_slug="password", env="NOTES_DB_PASSWORD").text) == "4"
    monkeypatch.setattr(Client, "shared_with_platform", lambda self, token, secret_id: False)
    response = post(secrets, "owner", "/apps/acme/notes/secrets", folder="17", secret_id="1042", field_slug="password", env="NOTES_DB_PASSWORD")
    assert response.status_code == 400 and "isn't shared with golden-road-eso" in html.unescape(response.text) and not bindings(secrets)


@pytest.mark.parametrize("method", ["secret_summary", "shared_with_platform", "template_fields"])
def test_an_outage_at_a_later_step_is_a_503_with_nothing_to_pick(secrets, monkeypatch, method):
    from portal.secretserver import Client, SecretServerError
    calls = {"n": 0}
    original = getattr(Client, method)

    def flaky(self, *a, **kw):   # answers step 3, fails once for step 4's dry run, then answers again
        calls["n"] += 1
        if calls["n"] == 2:
            raise SecretServerError("Secret Server failed (500)")
        return original(self, *a, **kw)
    monkeypatch.setattr(Client, method, flaky)
    response = get(secrets, folder="17", secret_id="1042", field_slug="password", env="NOTES_DB_PASSWORD")
    assert response.status_code == 503 and "Secret Server could not answer: try again." in response.text
    assert radios(response.text, "folder") == set() and "(500)" not in response.text


def test_a_refused_step_steps_down_one_at_a_time(secrets):
    """A refused name (step 4's dry run) shows step 3 with the secret still chosen, not step 1."""
    response = get(secrets, folder="17", secret_id="1042", field_slug="password", env="PORT")
    assert response.status_code == 400 and step_of(response.text) == "3" and "Using <span class=\"mono\">notes-db</span>" in response.text


def test_back_from_the_field_step_keeps_the_search(secrets):
    text = get(secrets, folder="17", q="notes", secret_id="1042").text
    assert step_of(text) == "3" and 'secrets/new?environment=test&amp;folder=17&amp;q=notes">Back</a>' in text
    assert '<input type="hidden" name="q" value="notes">' in text
