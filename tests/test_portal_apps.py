"""The app page (spec 2026-10-29 app bindings, "The portal"): /apps/<owner>/<repo>, its link, enable and remove forms, and what the
container gets. The `portal` fixture: owner admins acme/notes and acme/tools; the fake directory tags sp-notes for notes and grants
it wh-notes; sp-other and wh-other belong to acme/other."""

import copy
import html

import pytest
from portal_fakes import FAKE_DIRECTORY, approved_request, make_settings

from portal.directory import parse
from portal.store.base import Conflict, RepoAdmin


def post(portal, who, path, **form):
    return portal.client(who).post(path, data={"csrf_token": portal.csrfs[who], **form})


def bindings(portal, repo="acme/notes"):
    return [r for r in portal.store.requests_for_repo(repo) if r.kind == "binding"]


def merge_next(portal):
    """Walk the approved request the writer would claim next to merged, the way the writer drives the store."""
    store, now = portal.store, portal.clock()
    req = store.claim_approved(now=now)
    store.mark_pr_open(request_id=req.id, branch=f"portal/{req.id}", pr_number=7, pr_url="https://github.com/acme/scorecard-factory/pull/7",
                       head_sha="2" * 40, base_sha="1" * 40, now=now)
    return store.mark_merged(request_id=req.id, merge_sha="3" * 40, now=now)


def link_identity_and_merge(portal):
    assert post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes").status_code == 303
    portal.clock.advance(seconds=1)
    return merge_next(portal)


def link_databricks_and_merge(portal):
    assert post(portal, "owner", "/apps/acme/notes/databricks", warehouse_id="wh-notes").status_code == 303
    portal.clock.advance(seconds=1)
    return merge_next(portal)


def test_the_page_offers_only_the_repo_s_principals(portal):
    page = portal.client("owner").get("/apps/acme/notes")
    assert page.status_code == 200
    assert "Associate Azure Identity" in page.text and 'value="sp-notes"' in page.text and "sp-other" not in page.text
    assert "Enable Databricks Connection" in page.text and "Link an Azure identity first" in page.text
    assert "Nothing yet." in page.text


def test_linking_creates_an_approved_binding_request_and_shows_it_pending(portal):
    response = post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes")
    assert response.status_code == 303 and response.headers["location"] == "/apps/acme/notes?environment=test"
    [req] = bindings(portal)
    assert req.state == "approved" and req.final["azure_identity"]["directory_id"] == "sp-notes" and req.final["repo"] == "acme/notes"
    assert req.requested_by == portal.people["owner"].id and req.decided_by is None
    entry = next(e for e in portal.store.audit_entries() if e.subject_id == req.id and e.action == "request.created")
    assert entry.actor_id == portal.people["owner"].id and entry.before == {"binding": None}
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "pending" in page and "AZURE_CLIENT_ID" in page and "11111111-1111-4111-8111-111111111111" in page
    assert "login.microsoftonline.com" in page and "once the pending change is recorded" in page


def test_the_url_s_case_resolves_to_the_stored_name(portal):
    assert post(portal, "owner", "/apps/ACME/Notes/azure-identity", principal_id="sp-notes").headers["location"] == "/apps/acme/notes?environment=test"
    assert bindings(portal)[0].final["repo"] == "acme/notes"


def test_a_crafted_post_with_another_repo_s_principal_is_refused(portal):
    for principal_id in ("sp-other", "", "sp-nope"):
        response = post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id=principal_id)
        assert response.status_code == 400 and "isn't tagged for acme/notes" in html.unescape(response.text)
    assert not bindings(portal)


def test_databricks_needs_a_merged_identity_and_a_granted_warehouse(portal):
    response = post(portal, "owner", "/apps/acme/notes/databricks", warehouse_id="wh-notes")
    assert response.status_code == 400 and "Link an Azure identity first" in response.text
    link_identity_and_merge(portal)
    page = portal.client("owner").get("/apps/acme/notes").text
    assert 'value="wh-notes"' in page and "wh-other" not in page and "Linked: <span class=\"mono\">sp-notes</span>" in page
    response = post(portal, "owner", "/apps/acme/notes/databricks", warehouse_id="wh-other")
    assert response.status_code == 400 and "isn't granted to sp-notes" in html.unescape(response.text)
    assert post(portal, "owner", "/apps/acme/notes/databricks", warehouse_id="wh-notes").status_code == 303
    final = bindings(portal)[0].final
    assert final["databricks"]["directory_id"] == "wh-notes" and final["azure_identity"]["directory_id"] == "sp-notes"


def test_a_second_change_while_one_is_open_is_409(portal):
    post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes")
    response = post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes")
    assert response.status_code == 409 and "in progress" in response.text
    assert 'disabled' in portal.client("owner").get("/apps/acme/notes").text
    assert len(bindings(portal)) == 1


def test_a_race_the_store_catches_is_409_and_other_conflicts_are_bugs(portal, monkeypatch):
    def refuse(code):
        def create_binding(**kw):
            raise Conflict(code)
        return create_binding
    monkeypatch.setattr(portal.store, "create_binding", refuse("open_request_exists"))
    response = post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes")
    assert response.status_code == 409 and "in progress" in response.text
    monkeypatch.setattr(portal.store, "create_binding", refuse("request_exists"))
    with pytest.raises(Conflict):
        post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes")


def test_remove_identity_removes_databricks(portal):
    link_identity_and_merge(portal)
    link_databricks_and_merge(portal)
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "also removes the Databricks connection" in page and "DATABRICKS_HOST" in page and "adb-1111111111111111.1.azuredatabricks.net" in page
    assert post(portal, "owner", "/apps/acme/notes/remove", what="azure-identity").status_code == 303
    newest = bindings(portal)[0]
    assert newest.state == "approved" and "azure_identity" not in newest.final and "databricks" not in newest.final
    assert newest.final["linked_by"] == portal.people["owner"].id
    portal.clock.advance(seconds=1)
    merge_next(portal)
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "Nothing yet." in page and "AZURE_CLIENT_ID" not in page and "Link an Azure identity first" in page


def test_remove_databricks_keeps_the_identity(portal):
    link_identity_and_merge(portal)
    link_databricks_and_merge(portal)
    assert post(portal, "owner", "/apps/acme/notes/remove", what="databricks").status_code == 303
    final = bindings(portal)[0].final
    assert "databricks" not in final and final["azure_identity"]["directory_id"] == "sp-notes"


def test_removing_what_isn_t_linked_is_refused(portal):
    for what in ("azure-identity", "databricks", "everything", ""):
        assert post(portal, "owner", "/apps/acme/notes/remove", what=what).status_code == 400, what
    assert not bindings(portal)


def test_a_failed_change_shows_its_reason_and_can_be_tried_again(portal):
    post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes")
    req = portal.store.claim_approved(now=portal.clock())
    portal.store.mark_failed(request_id=req.id, reason="the factory refused <the file>", now=portal.clock())
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "could not be recorded: the factory refused &lt;the file&gt;" in page and "Nothing yet." in page
    portal.clock.advance(seconds=1)
    assert post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes").status_code == 303
    assert "could not be recorded" not in portal.client("owner").get("/apps/acme/notes").text


def test_the_registration_shows_when_the_portal_merged_one(portal):
    assert "No portal registration; the writer checks the factory's intake record before it records a change." in portal.client("owner").get("/apps/acme/notes").text
    req = approved_request(portal.store, portal.people, portal.clock())
    portal.store.mark_pr_open(request_id=req.id, branch="b", pr_number=1, pr_url="https://github.com/acme/scorecard-factory/pull/1",
                              head_sha="2" * 40, base_sha="1" * 40, now=portal.clock())
    portal.store.mark_merged(request_id=req.id, merge_sha="3" * 40, now=portal.clock())
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "No portal registration; the writer checks the factory's intake record before it records a change." not in page and "moderate" in page and "2027-10-23" in page and "Stores short team notes" in page


def test_a_portal_admin_sees_the_page_but_only_the_repo_s_admin_changes_it(portal):
    page = portal.client("admin").get("/apps/acme/notes")
    assert page.status_code == 200 and "Associate Azure Identity" in page.text and 'value="sp-notes"' not in page.text
    assert "Only an admin of the repository, as the portal last saw it, can change its connections" in html.unescape(page.text)
    response = post(portal, "admin", "/apps/acme/notes/azure-identity", principal_id="sp-notes")
    assert response.status_code == 403 and not bindings(portal)


def test_without_a_directory_the_options_are_hidden_and_posts_404(portal):
    portal.app.state.settings = make_settings()   # directory None
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "Associate Azure Identity" not in page and "Connections are not available on this portal" in page
    for path, form in (("azure-identity", {"principal_id": "sp-notes"}), ("databricks", {"warehouse_id": "wh-notes"}), ("remove", {"what": "databricks"})):
        assert post(portal, "owner", f"/apps/acme/notes/{path}", **form).status_code == 404
    assert not bindings(portal)


def test_every_form_needs_csrf(portal):
    for path, form in (("azure-identity", {"principal_id": "sp-notes"}), ("databricks", {"warehouse_id": "wh-notes"}), ("remove", {"what": "databricks"})):
        assert portal.client("owner").post(f"/apps/acme/notes/{path}", data=form).status_code == 403
        assert portal.client("owner").post(f"/apps/acme/notes/{path}", data={**form, "csrf_token": "wrong"}).status_code == 403
    assert not bindings(portal)


def test_directory_text_is_escaped(portal):
    doc = copy.deepcopy(FAKE_DIRECTORY)
    doc["principals"][0]["display_name"] = "<script>x</script>"
    portal.app.state.settings = make_settings(directory=parse(doc))
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "<script>x</script>" not in page and "&lt;script&gt;x&lt;/script&gt;" in page


def test_the_process_links_come_from_settings(portal):
    portal.app.state.settings = make_settings(directory=portal.settings.directory, identity_process_url="https://wiki.example.test/sp",
                                              data_process_url="https://wiki.example.test/wh")
    page = portal.client("owner").get("/apps/acme/notes").text
    assert '<a href="https://wiki.example.test/sp" rel="noopener noreferrer">' in page
    link_identity_and_merge(portal)
    assert '<a href="https://wiki.example.test/wh" rel="noopener noreferrer">' in portal.client("owner").get("/apps/acme/notes").text


def test_others_get_404(portal):
    assert portal.client("other").get("/apps/acme/notes").status_code == 404
    assert post(portal, "other", "/apps/acme/notes/azure-identity", principal_id="sp-notes").status_code == 404
    assert portal.client("owner").get("/apps/acme/other").status_code == 404
    assert not bindings(portal)


def test_admins_see_the_whole_directory(portal):
    page = portal.client("admin").get("/admin/directory")
    assert page.status_code == 200 and "sp-other" in page.text and "wh-other" in page.text and "acme/notes" in page.text
    assert "/sql/1.0/warehouses/0000other0000" in page.text and "This is the mock directory" in page.text
    assert portal.client("owner").get("/admin/directory").status_code == 403
    portal.app.state.settings = make_settings()
    assert "The directory is off on this portal" in portal.client("admin").get("/admin/directory").text


def test_my_requests_lists_my_apps_and_binding_requests(portal):
    page = portal.client("owner").get("/requests").text
    assert 'href="/apps/acme/notes"' in page and 'href="/apps/acme/tools"' in page and "My apps" in page
    post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes")
    page = portal.client("owner").get("/requests").text
    [req] = bindings(portal)
    assert "Connections" in page and "Registration" in page and f"/requests/{req.id}" not in page
    assert portal.client("other").get("/requests").text.count("/apps/") == 0


def test_a_binding_request_s_page_is_the_app_page(portal):
    post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes")
    [req] = bindings(portal)
    for who in ("owner", "admin"):
        response = portal.client(who).get(f"/requests/{req.id}")
        assert response.status_code == 303 and response.headers["location"] == "/apps/acme/notes?environment=test"
    assert portal.client("other").get(f"/requests/{req.id}").status_code == 404


def test_admin_actions_on_a_binding_request_are_refused(portal, monkeypatch):
    import portal.web.routes_admin as routes_admin
    def no_intake(*a, **kw):
        raise AssertionError("a binding request is never made into an intake record")
    monkeypatch.setattr(routes_admin, "intake_record", no_intake)
    post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes")
    [req] = bindings(portal)
    approve = post(portal, "admin", f"/admin/requests/{req.id}/approve", tier="low", data_classification="internal", review_by="2027-10-23",
                   justification="Purpose and classification checked.", submission="1")
    send_back = post(portal, "admin", f"/admin/requests/{req.id}/send-back", justification="Please change the thing.")
    resubmit = post(portal, "owner", f"/requests/{req.id}", purpose="x" * 30, data_classification="internal", proposed_tier="low")
    assert (approve.status_code, send_back.status_code, resubmit.status_code) == (409, 409, 409)
    assert "Only a registration is approved here" in approve.text
    assert portal.store.get_request(req.id).state == "approved" and portal.store.get_request(req.id).decided_by is None


def test_a_binding_row_links_to_the_app_only_while_i_admin_it(portal):
    post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes")
    portal.store.replace_repo_admins([RepoAdmin("acme/tools", "owner-gh", 1001, portal.clock())], observed_at=portal.clock())
    page = portal.client("owner").get("/requests").text
    assert 'href="/apps/acme/notes"' not in page and "acme/notes" in page and 'href="/apps/acme/tools"' in page


def test_a_stale_observation_can_t_change_connections(portal):
    portal.clock.advance(minutes=11)  # past repo_admins_stale_after; the session stays valid
    assert post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes").status_code == 404
    assert not bindings(portal)


def test_a_portal_admin_who_admins_the_repo_changes_its_connections(portal):
    now = portal.clock()
    portal.store.add_link(person_id=portal.people["admin"].id, github_login="admin-gh", github_user_id=1002, source="admin",
                          actor_id=portal.people["owner"].id, reason="seeded for the test", now=now)
    portal.store.replace_repo_admins([RepoAdmin("acme/notes", "owner-gh", 1001, now), RepoAdmin("acme/notes", "admin-gh", 1002, now)], observed_at=now)
    assert 'value="sp-notes"' in portal.client("admin").get("/apps/acme/notes").text
    assert post(portal, "admin", "/apps/acme/notes/azure-identity", principal_id="sp-notes").status_code == 303
    [req] = bindings(portal)
    assert req.requested_by == portal.people["admin"].id and req.final["linked_by"] == portal.people["admin"].id



# ---- the environment selector (spec 2026-10-29 GitOps deploy repo, G9): test and prod have their own connections ----

def test_the_default_environment_is_test(portal):
    page = portal.client("owner").get("/apps/acme/notes").text
    assert 'href="/apps/acme/notes?environment=test" aria-current="page"' in page and 'href="/apps/acme/notes?environment=prod"' in page
    assert 'name="environment" value="test"' in page and 'name="environment" value="prod"' not in page
    assert post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes").status_code == 303
    assert bindings(portal)[0].final["environment"] == "test"


def test_prod_changes_don_t_touch_test(portal):
    response = post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes", environment="prod")
    assert response.status_code == 303 and response.headers["location"] == "/apps/acme/notes?environment=prod"
    [req] = bindings(portal)
    assert req.final["environment"] == "prod" and req.final["schema"] == "binding/v2"
    prod = portal.client("owner").get("/apps/acme/notes?environment=prod").text
    assert 'name="environment" value="prod"' in prod and "pending" in prod and "AZURE_CLIENT_ID" in prod
    assert 'href="/apps/acme/notes?environment=prod" aria-current="page"' in prod
    test = portal.client("owner").get("/apps/acme/notes?environment=test").text
    assert "pending" not in test and "AZURE_CLIENT_ID" not in test and "Nothing yet." in test
    # test may change while prod's change is open, and its own open change blocks only test
    assert post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes", environment="test").status_code == 303
    assert post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes", environment="test").status_code == 409
    assert post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes", environment="prod").status_code == 409
    assert sorted(r.final["environment"] for r in bindings(portal)) == ["prod", "test"]


def test_each_environment_has_its_own_current_binding(portal):
    post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes", environment="prod")
    portal.clock.advance(seconds=1)
    merge_next(portal)
    assert post(portal, "owner", "/apps/acme/notes/databricks", warehouse_id="wh-notes", environment="test").status_code == 400  # test has no identity
    assert post(portal, "owner", "/apps/acme/notes/databricks", warehouse_id="wh-notes", environment="prod").status_code == 303
    prod = portal.client("owner").get("/apps/acme/notes?environment=prod").text
    assert 'Linked: <span class="mono">sp-notes</span>' in prod and "DATABRICKS_HOST" in prod
    assert 'Linked: <span class="mono">sp-notes</span>' not in portal.client("owner").get("/apps/acme/notes").text


@pytest.mark.parametrize("environment", ["staging", "", "Prod", "prod ", "test\n"])
def test_a_bad_environment_is_a_400(portal, environment):
    assert portal.client("owner").get("/apps/acme/notes", params={"environment": environment}).status_code == 400
    assert portal.client("admin").get("/apps/acme/notes", params={"environment": environment}).status_code == 400
    for path, form in (("azure-identity", {"principal_id": "sp-notes"}), ("databricks", {"warehouse_id": "wh-notes"}), ("remove", {"what": "databricks"})):
        assert post(portal, "owner", f"/apps/acme/notes/{path}", environment=environment, **form).status_code == 400
    assert not bindings(portal)


def test_a_failed_change_shows_only_on_its_environment(portal):
    post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes", environment="prod")
    req = portal.store.claim_approved(now=portal.clock())
    portal.store.mark_failed(request_id=req.id, reason="not_deployed", now=portal.clock())
    assert "could not be recorded: not_deployed" in portal.client("owner").get("/apps/acme/notes?environment=prod").text
    assert "could not be recorded" not in portal.client("owner").get("/apps/acme/notes?environment=test").text


def test_a_prod_binding_request_s_page_is_the_prod_app_page(portal):
    post(portal, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes", environment="prod")
    [req] = bindings(portal)
    response = portal.client("owner").get(f"/requests/{req.id}")
    assert response.status_code == 303 and response.headers["location"] == "/apps/acme/notes?environment=prod"
    page = portal.client("owner").get("/requests").text
    assert 'href="/apps/acme/notes?environment=prod"' in page and "Connections (prod)" in page


# ---- Delinea Secret Server secrets (spec 2026-10-29 Delinea secrets, "The portal") ----
# The mock user mwdavisii (folders 17, 18) sees 1042 notes-db, 1043 notes-api-key (not shared with golden-road-eso), 1044 notes-tls
# (a certificate file field and a passphrase) and 1050 portal-smtp; 2001 finance-db is the mock user other's.

import json  # noqa: E402

from portal.secretserver_mock import MOCK, load_mock, parse as parse_mock  # noqa: E402


def with_secret_server(portal, secret_server=None, link=True, **settings):
    portal.app.state.settings = make_settings(directory=portal.settings.directory, secret_server=secret_server or load_mock(), **settings)
    if link:  # the person's own OAuth-verified link: the one kind that maps them to a Secret Server user
        portal.store.add_link(person_id=portal.people["owner"].id, github_login="mwdavisii", github_user_id=2002, source="github-oauth",
                              actor_id=portal.people["owner"].id, reason="linked through GitHub OAuth", now=portal.clock())
    return portal


@pytest.fixture
def secrets(portal):
    return with_secret_server(portal)


def add_secret(portal, secret_id="1042", field_slug="password", env="NOTES_DB_PASSWORD", who="owner", **form):
    return post(portal, who, "/apps/acme/notes/secrets", secret_id=secret_id, field_slug=field_slug, env=env, **form)


def add_and_merge(portal, **form):
    assert add_secret(portal, **form).status_code == 303
    portal.clock.advance(seconds=1)
    return merge_next(portal)


def secret_rows(text):
    return {int(m) for m in __import__("re").findall(r'name="secret_id" value="(\d+)"', text)}


def test_the_secrets_section_is_hidden_without_secret_server(portal):
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "Secrets (Delinea Secret Server)" not in page and "/secrets" not in page
    response = add_secret(portal)
    assert response.status_code == 400 and "Secret Server is not set up here" in response.text and not bindings(portal)


def test_the_search_lists_what_the_person_can_see_and_marks_the_unshared(secrets):
    page = secrets.client("owner").get("/apps/acme/notes", params={"q": ""}).text
    assert "Secrets (Delinea Secret Server)" in page
    for name in ("notes-db", "notes-api-key", "notes-tls", "portal-smtp"):
        assert name in page
    assert "finance-db" not in page and "2001" not in page
    assert secret_rows(page) == {1042, 1044, 1050}   # 1043 has no form: it isn't shared with golden-road-eso
    assert "Ask security to share it with" in page and "golden-road-eso" in page
    assert '<option value="passphrase">' in page and 'value="certificate"' not in page
    assert 'pattern="[A-Z_][A-Z0-9_]*"' in page and 'maxlength="128"' in page
    assert "\\Golden Road\\hello-app" in html.unescape(page)


def test_the_search_filters_by_text_and_folder(secrets):
    client = secrets.client("owner")
    assert secret_rows(client.get("/apps/acme/notes", params={"q": "smtp"}).text) == {1050}
    assert secret_rows(client.get("/apps/acme/notes", params={"folder": "18"}).text) == {1050}
    assert secret_rows(client.get("/apps/acme/notes", params={"folder": "30"}).text) == set()   # not the person's folder
    page = client.get("/apps/acme/notes", params={"q": "x" * 101})
    assert page.status_code == 400 and "at most 100 characters" in page.text and secret_rows(page.text) == set()
    page = client.get("/apps/acme/notes", params={"folder": "１８"})
    assert page.status_code == 400 and secret_rows(page.text) == set()


def test_adding_a_secret_creates_the_request(secrets):
    response = add_secret(secrets)
    assert response.status_code == 303 and response.headers["location"] == "/apps/acme/notes?environment=test"
    [req] = bindings(secrets)
    assert req.state == "approved" and req.final["secrets"] == [
        {"env": "NOTES_DB_PASSWORD", "secret_id": 1042, "secret_name": "notes-db", "folder_id": 17, "field_slug": "password",
         "verified_by": "mock-secret-server"}]
    page = secrets.client("owner").get("/apps/acme/notes").text
    assert "NOTES_DB_PASSWORD" in page and "from Delinea Secret Server: notes-db / password" in page


@pytest.mark.parametrize("form, message", [
    ({"secret_id": "2001"}, "You can't see this secret"),
    ({"secret_id": "9999"}, "You can't see this secret"),
    ({"secret_id": "1043", "field_slug": "api-key"}, "isn't shared with golden-road-eso"),
    ({"secret_id": "1044", "field_slug": "certificate"}, "not a field this secret can deliver"),
    ({"env": "PORT"}, "set by the platform"),
    ({"env": "AZURE_CLIENT_ID"}, "set by the platform"),
    ({"env": "lower"}, "Name the variable"),
    ({"secret_id": "abc"}, "Choose a secret"),
    ({"secret_id": "01042"}, "Choose a secret"),
    ({"secret_id": ""}, "Choose a secret"),
    ({"field_slug": ""}, "Choose a secret"),
])
def test_crafted_posts_are_refused_and_nothing_is_stored(secrets, form, message):
    response = add_secret(secrets, **form)
    assert response.status_code == 400 and message in html.unescape(response.text) and not bindings(secrets)


def test_a_duplicate_env_and_a_21st_secret_are_refused(secrets):
    add_and_merge(secrets)
    response = add_secret(secrets, secret_id="1050", field_slug="username")
    assert response.status_code == 400 and "NOTES_DB_PASSWORD already holds a secret" in response.text and len(bindings(secrets)) == 1
    for i in range(19):
        add_and_merge(secrets, env=f"S{i:02d}")
    assert len(bindings(secrets)[0].final["secrets"]) == 20
    response = add_secret(secrets, env="S99")
    assert response.status_code == 400 and "at most 20 secrets" in response.text and len(bindings(secrets)) == 20


def test_removing_a_secret(secrets):
    add_and_merge(secrets)
    page = secrets.client("owner").get("/apps/acme/notes").text
    assert 'action="/apps/acme/notes/secrets/remove"' in page and 'name="env" value="NOTES_DB_PASSWORD"' in page
    assert post(secrets, "owner", "/apps/acme/notes/secrets/remove", env="NOPE").status_code == 400
    response = post(secrets, "owner", "/apps/acme/notes/secrets/remove", env="NOTES_DB_PASSWORD")
    assert response.status_code == 303 and bindings(secrets)[0].final["secrets"] == []


def test_no_value_and_no_password_reaches_a_page(secrets):
    add_and_merge(secrets)
    add_secret(secrets, secret_id="1044", field_slug="passphrase", env="TLS_PASSPHRASE")
    pages = [secrets.client(who).get("/apps/acme/notes", params=params).text for who in ("owner", "admin")
             for params in ({}, {"environment": "prod"}, {"q": "notes"}, {"q": ""}, {"folder": "17"})]
    pages.append(add_secret(secrets, secret_id="2001").text)
    for text in pages:
        assert "&lt;mock&gt;" not in text and "<mock>" not in text and "itemValue" not in text and "mock-golden-road-eso" not in text
        assert "mock-mwdavisii" not in text


def test_a_hostile_secret_name_is_escaped(portal):
    doc = json.loads(MOCK.read_text())
    doc["secrets"][0]["name"] = "<script>alert(1)</script>"
    with_secret_server(portal, parse_mock(doc))
    page = portal.client("owner").get("/apps/acme/notes", params={"q": ""}).text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    response = add_secret(portal)
    assert response.status_code == 400 and "can't be recorded in a binding" in html.unescape(response.text) and not bindings(portal)


def test_without_a_verified_link_the_section_says_why(portal):
    with_secret_server(portal, link=False)   # owner has only the seeded, admin-made link
    page = portal.client("owner").get("/apps/acme/notes", params={"q": ""}).text
    assert "Link your GitHub account (verified)" in html.unescape(page) and secret_rows(page) == set()
    response = add_secret(portal)
    assert response.status_code == 400 and "Link your GitHub account (verified) first" in html.unescape(response.text)
    assert not bindings(portal)


def test_a_secret_server_failure_is_a_plain_note(secrets, monkeypatch):
    from portal.secretserver import Client, SecretServerError

    def fail(self, *a, **kw):
        raise SecretServerError("Secret Server failed (500) answering for /api/v1/secrets")
    monkeypatch.setattr(Client, "search_first", fail)
    page = secrets.client("owner").get("/apps/acme/notes", params={"q": ""})
    assert page.status_code == 200 and "Secret Server could not answer: try again" in page.text and "(500)" not in page.text


def test_the_security_process_link_comes_from_settings(portal):
    with_secret_server(portal, secret_process_url="https://wiki.example.test/share")
    assert '<a href="https://wiki.example.test/share" rel="noopener noreferrer">' in portal.client("owner").get("/apps/acme/notes").text


def test_a_portal_admin_sees_the_secrets_but_can_t_search_or_add(secrets):
    add_and_merge(secrets)
    page = secrets.client("admin").get("/apps/acme/notes", params={"q": ""}).text
    assert "NOTES_DB_PASSWORD" in page and secret_rows(page) == set() and "/secrets/remove" not in page
    assert add_secret(secrets, who="admin", env="OTHER").status_code == 403
    assert post(secrets, "admin", "/apps/acme/notes/secrets/remove", env="NOTES_DB_PASSWORD").status_code == 403


def test_strangers_get_404_on_the_secret_posts(secrets):
    assert add_secret(secrets, who="other").status_code == 404
    assert post(secrets, "other", "/apps/acme/notes/secrets/remove", env="NOTES_DB_PASSWORD").status_code == 404
    assert not bindings(secrets)


def test_the_secret_posts_need_csrf(secrets):
    for path, form in (("secrets", {"secret_id": "1042", "field_slug": "password", "env": "X"}), ("secrets/remove", {"env": "X"})):
        assert secrets.client("owner").post(f"/apps/acme/notes/{path}", data=form).status_code == 403
        assert secrets.client("owner").post(f"/apps/acme/notes/{path}", data={**form, "csrf_token": "wrong"}).status_code == 403
    assert not bindings(secrets)


def test_prod_and_test_secrets_are_separate(secrets):
    response = add_secret(secrets, environment="prod")
    assert response.status_code == 303 and response.headers["location"] == "/apps/acme/notes?environment=prod"
    [req] = bindings(secrets)
    assert req.final["environment"] == "prod"
    assert "from Delinea Secret Server: notes-db / password" in secrets.client("owner").get("/apps/acme/notes?environment=prod").text
    assert "from Delinea Secret Server" not in secrets.client("owner").get("/apps/acme/notes").text
    assert add_secret(secrets, environment="test").status_code == 303   # test's own change, while prod's is open
    assert add_secret(secrets, environment="staging").status_code == 400


def search_calls(monkeypatch):
    from portal.secretserver import Client
    calls = []
    for method in ("search_first", "search_secrets"):
        original = getattr(Client, method)

        def spy(self, *a, _original=original, **kw):
            calls.append(kw)
            return _original(self, *a, **kw)
        monkeypatch.setattr(Client, method, spy)
    return calls


def test_secret_server_is_searched_only_when_a_search_is_submitted(secrets, monkeypatch):
    calls = search_calls(monkeypatch)
    page = secrets.client("owner").get("/apps/acme/notes").text
    assert calls == [] and secret_rows(page) == set() and "Search to list the secrets you can see" in page
    assert 'name="q"' in page and '<option value="17">' in page   # the form and the person's folders are still there
    assert add_secret(secrets, env="PORT").status_code == 400 and calls == []   # an error re-render does not search
    assert secrets.client("owner").get("/apps/acme/notes", params={"q": "x" * 101}).status_code == 400 and calls == []
    secrets.client("owner").get("/apps/acme/notes", params={"q": "notes"})
    assert calls == [{"text": "notes", "folder_id": None, "take": 20}]


def test_one_page_of_at_most_20_results_is_shown(portal):
    doc = json.loads(MOCK.read_text())
    doc["secrets"] += [{"id": 3000 + i, "name": f"bulk-{i:02d}", "folderId": 17, "secretTemplateId": 6001, "shared_with": []}
                       for i in range(30)]
    with_secret_server(portal, parse_mock(doc))
    page = portal.client("owner").get("/apps/acme/notes", params={"q": "bulk"}).text
    assert page.count("Ask security to share it with") == 20 and "Only the first 20 are shown: narrow the search" in page
    page = portal.client("owner").get("/apps/acme/notes", params={"q": "notes"}).text
    assert "Only the first" not in page


def test_secrets_work_without_the_directory(portal):
    with_secret_server(portal)   # adds the person's verified link
    portal.app.state.settings = make_settings(secret_server=load_mock())   # directory off, Secret Server on
    page = portal.client("owner").get("/apps/acme/notes", params={"q": ""}).text
    assert "Secrets (Delinea Secret Server)" in page and "Associate Azure Identity" not in page and secret_rows(page) == {1042, 1044, 1050}
    assert "Connections are not available on this portal" not in page
    for path, form in (("azure-identity", {"principal_id": "sp-notes"}), ("databricks", {"warehouse_id": "wh-notes"}),
                       ("remove", {"what": "databricks"})):
        assert post(portal, "owner", f"/apps/acme/notes/{path}", **form).status_code == 404   # these still need the directory
    add_and_merge(portal)
    assert bindings(portal)[0].final["secrets"][0]["env"] == "NOTES_DB_PASSWORD"
    assert "from Delinea Secret Server: notes-db / password" in portal.client("owner").get("/apps/acme/notes").text
    assert post(portal, "owner", "/apps/acme/notes/secrets/remove", env="NOTES_DB_PASSWORD").status_code == 303
    assert bindings(portal)[0].final["secrets"] == []


def test_with_neither_directory_nor_secret_server_every_change_is_404(portal):
    portal.app.state.settings = make_settings()
    assert "Connections are not available on this portal" in portal.client("owner").get("/apps/acme/notes").text
    assert add_secret(portal).status_code == 404
    assert post(portal, "owner", "/apps/acme/notes/secrets/remove", env="X").status_code == 404
    assert not bindings(portal)


def test_with_the_directory_but_no_secret_server_secret_posts_are_refused(portal):
    for response in (add_secret(portal), post(portal, "owner", "/apps/acme/notes/secrets/remove", env="NOTES_DB_PASSWORD")):
        assert response.status_code == 400 and "Secret Server is not set up here" in response.text
    assert not bindings(portal)


def test_egress_shows_for_an_identity_with_or_without_secrets_and_not_for_secrets_alone(secrets):
    add_and_merge(secrets)
    page = secrets.client("owner").get("/apps/acme/notes").text
    assert "from Delinea Secret Server: notes-db / password" in page and "Egress on 443" not in page and "AZURE_CLIENT_ID" not in page
    link_identity_and_merge(secrets)
    page = secrets.client("owner").get("/apps/acme/notes").text
    assert "Egress on 443 to: <span class=\"mono\">login.microsoftonline.com</span>" in page and "AZURE_CLIENT_ID" in page
    assert "from Delinea Secret Server: notes-db / password" in page


def test_an_identity_alone_still_shows_its_egress(portal):
    link_identity_and_merge(portal)
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "Egress on 443 to: <span class=\"mono\">login.microsoftonline.com</span>" in page and "from Delinea Secret Server" not in page
