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
    assert 'value="wh-notes"' in page and "wh-other" not in page and "<dt>Principal</dt><dd class=\"mono\">sp-notes</dd>" in page
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
    assert post(portal, "owner", "/apps/acme/notes/remove", what="azure-identity", confirmed="yes").status_code == 303
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
    assert post(portal, "owner", "/apps/acme/notes/remove", what="databricks", confirmed="yes").status_code == 303
    final = bindings(portal)[0].final
    assert "databricks" not in final and final["azure_identity"]["directory_id"] == "sp-notes"


def test_removing_what_isn_t_linked_is_refused(portal):
    for what in ("azure-identity", "databricks", "everything", ""):
        assert post(portal, "owner", "/apps/acme/notes/remove", what=what, confirmed="yes").status_code == 400, what
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
    approve = post(portal, "admin", f"/admin/requests/{req.id}/approve", tier="low", data_classification="public", review_by="2027-10-23",
                   justification="Purpose and classification checked.", submission="1")
    send_back = post(portal, "admin", f"/admin/requests/{req.id}/send-back", justification="Please change the thing.")
    resubmit = post(portal, "owner", f"/requests/{req.id}", purpose="x" * 30, data_classification="public")
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
    assert '<dt>Principal</dt><dd class="mono">sp-notes</dd>' in prod and "DATABRICKS_HOST" in prod
    assert '<dt>Principal</dt><dd class="mono">sp-notes</dd>' not in portal.client("owner").get("/apps/acme/notes").text


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


def add_secret(portal, secret_id="1042", field_slug="password", env="NOTES_DB_PASSWORD", who="owner", folder="17", **form):
    return post(portal, who, "/apps/acme/notes/secrets", folder=folder, secret_id=secret_id, field_slug=field_slug, env=env, **form)


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
    page = secrets.client("owner").get("/apps/acme/notes", params={"confirm_secret": "NOTES_DB_PASSWORD"}).text
    assert 'action="/apps/acme/notes/secrets/remove"' in page and 'name="env" value="NOTES_DB_PASSWORD"' in page
    assert post(secrets, "owner", "/apps/acme/notes/secrets/remove", env="NOPE", confirmed="yes").status_code == 400
    response = post(secrets, "owner", "/apps/acme/notes/secrets/remove", env="NOTES_DB_PASSWORD", confirmed="yes")
    assert response.status_code == 303 and bindings(secrets)[0].final["secrets"] == []


def test_no_value_and_no_password_reaches_a_page(secrets):
    add_and_merge(secrets)
    pages = [secrets.client(who).get(path, params=params).text for who, path, params in (
        ("owner", "/apps/acme/notes", {}), ("owner", "/apps/acme/notes", {"environment": "prod"}), ("admin", "/apps/acme/notes", {}),
        ("owner", "/apps/acme/notes/secrets/new", {}), ("owner", "/apps/acme/notes/secrets/new", {"folder": "17"}),
        ("owner", "/apps/acme/notes/secrets/new", {"folder": "17", "secret_id": "1044"}),
        ("owner", "/apps/acme/notes/secrets/new", {"folder": "17", "secret_id": "1044", "field_slug": "passphrase", "env": "TLS_PASSPHRASE2"}))]
    add_secret(secrets, secret_id="1044", field_slug="passphrase", env="TLS_PASSPHRASE")
    pages.append(add_secret(secrets, secret_id="2001").text)
    for text in pages:
        assert "&lt;mock&gt;" not in text and "<mock>" not in text and "itemValue" not in text and "mock-golden-road-eso" not in text
        assert "mock-mwdavisii" not in text


def test_a_hostile_secret_name_is_escaped(portal):
    doc = json.loads(MOCK.read_text())
    doc["secrets"][0]["name"] = "<script>alert(1)</script>"
    with_secret_server(portal, parse_mock(doc))   # the page half is in test_portal_secret_steps.py
    response = add_secret(portal)
    assert response.status_code == 400 and "can't be recorded in a binding" in html.unescape(response.text) and not bindings(portal)


def test_without_a_verified_link_the_section_says_why(portal):
    with_secret_server(portal, link=False)   # owner has only the seeded, admin-made link; the steps' note is tested there
    response = add_secret(portal)
    assert response.status_code == 400 and "Link your GitHub account (verified) first" in html.unescape(response.text)
    assert not bindings(portal)


def test_the_security_process_link_comes_from_settings(portal):
    with_secret_server(portal, secret_process_url="https://wiki.example.test/share")
    assert '<a href="https://wiki.example.test/share" rel="noopener noreferrer">' in portal.client("owner").get("/apps/acme/notes").text


def test_a_portal_admin_sees_the_secrets_but_can_t_search_or_add(secrets):
    add_and_merge(secrets)
    page = secrets.client("admin").get("/apps/acme/notes").text
    assert "NOTES_DB_PASSWORD" in page and "/secrets/new" not in page and "confirm_secret" not in page
    assert add_secret(secrets, who="admin", env="OTHER").status_code == 403
    assert post(secrets, "admin", "/apps/acme/notes/secrets/remove", env="NOTES_DB_PASSWORD").status_code == 403
    assert secrets.client("admin").get("/apps/acme/notes/secrets/new").status_code == 403


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


def test_secrets_work_without_the_directory(portal):
    with_secret_server(portal)   # adds the person's verified link
    portal.app.state.settings = make_settings(secret_server=load_mock())   # directory off, Secret Server on
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "Secrets (Delinea Secret Server)" in page and "Associate Azure Identity" not in page
    assert 'href="/apps/acme/notes/secrets/new?environment=test"' in page
    steps = portal.client("owner").get("/apps/acme/notes/secrets/new", params={"folder": "17"}).text
    assert {1042, 1044} <= secret_rows(steps)
    assert "Connections are not available on this portal" not in page
    for path, form in (("azure-identity", {"principal_id": "sp-notes"}), ("databricks", {"warehouse_id": "wh-notes"}),
                       ("remove", {"what": "databricks"})):
        assert post(portal, "owner", f"/apps/acme/notes/{path}", **form).status_code == 404   # these still need the directory
    add_and_merge(portal)
    assert bindings(portal)[0].final["secrets"][0]["env"] == "NOTES_DB_PASSWORD"
    assert "from Delinea Secret Server: notes-db / password" in portal.client("owner").get("/apps/acme/notes").text
    assert post(portal, "owner", "/apps/acme/notes/secrets/remove", env="NOTES_DB_PASSWORD", confirmed="yes").status_code == 303
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


# ---- the reskin: cards and a confirmation before a removal (spec 2026-10-30 R6) ----

def test_the_page_is_cards_with_the_registration_summary_and_the_tabs(portal):
    text = portal.client("owner").get("/apps/acme/notes").text
    assert '<p><a href="/requests">&larr; My requests</a></p>' in text and '<nav class="tabs" aria-label="Environment">' in text
    assert text.count('<section class="card connection">') == 2   # the Azure identity and Databricks (no Secret Server here)
    assert '<section class="card">\n<h3>What the container gets</h3>' in text


def test_a_removal_asks_first_and_only_the_confirming_post_removes(portal):
    link_identity_and_merge(portal)
    page = portal.client("owner").get("/apps/acme/notes").text
    assert 'href="/apps/acme/notes?environment=test&amp;confirm=azure-identity"' in page
    assert 'action="/apps/acme/notes/remove"' not in page   # no removal form until it is confirmed
    ask = portal.client("owner").get("/apps/acme/notes", params={"confirm": "azure-identity"})
    assert ask.status_code == 200 and "Remove the Azure identity?" in ask.text
    assert 'name="what" value="azure-identity"' in ask.text and 'name="confirmed" value="yes"' in ask.text
    assert 'href="/apps/acme/notes?environment=test">Keep it</a>' in ask.text and len(bindings(portal)) == 1   # asking changes nothing
    refused = post(portal, "owner", "/apps/acme/notes/remove", what="azure-identity")
    assert refused.status_code == 400 and "Confirm the removal first" in refused.text and len(bindings(portal)) == 1
    assert post(portal, "owner", "/apps/acme/notes/remove", what="azure-identity", confirmed="yes").status_code == 303
    assert len(bindings(portal)) == 2


@pytest.mark.parametrize("params", [{"confirm": "databricks"}, {"confirm": "everything"}, {"confirm_secret": "NOPE"}])
def test_asking_to_remove_what_isn_t_there_is_a_400(portal, params):
    link_identity_and_merge(portal)   # an identity, no Databricks, no secrets
    response = portal.client("owner").get("/apps/acme/notes", params=params)
    assert response.status_code == 400 and "There is nothing to remove." in response.text


def test_while_a_change_is_pending_remove_is_disabled_with_the_reason(portal):
    link_identity_and_merge(portal)
    assert post(portal, "owner", "/apps/acme/notes/databricks", warehouse_id="wh-notes").status_code == 303   # pending now
    page = portal.client("owner").get("/apps/acme/notes").text
    assert '<button class="danger" type="button" disabled>Remove</button>' in page and "Changes are paused until it lands." in page
    assert portal.client("owner").get("/apps/acme/notes", params={"confirm": "azure-identity"}).status_code == 409


def test_only_the_repo_s_admin_is_asked(portal):
    link_identity_and_merge(portal)
    assert portal.client("admin").get("/apps/acme/notes", params={"confirm": "azure-identity"}).status_code == 403


def test_the_confirmation_page_is_free_of_inline_style_and_script(portal):
    from test_portal_csp_pages import problems
    link_identity_and_merge(portal)
    assert problems(portal.client("owner").get("/apps/acme/notes", params={"confirm": "azure-identity"}).text) == []


def test_a_linked_connection_can_be_removed_after_its_tag_or_grant_is_gone(portal):
    """Round 6 review: Remove doesn't depend on the directory still offering the principal or the warehouse."""
    link_identity_and_merge(portal)
    link_databricks_and_merge(portal)
    doc = copy.deepcopy(FAKE_DIRECTORY)
    doc["principals"][0]["tagged_repo_name"] = "elsewhere"   # sp-notes untagged
    for w in doc["databricks"]:
        w["granted_to"] = ["sp-other"]   # nothing granted to sp-notes any more
    portal.app.state.settings = make_settings(directory=parse(doc))
    page = portal.client("owner").get("/apps/acme/notes").text
    assert 'href="/apps/acme/notes?environment=test&amp;confirm=azure-identity"' in page
    assert 'href="/apps/acme/notes?environment=test&amp;confirm=databricks"' in page
    assert "No service principal is tagged for this repository yet." not in page
    assert "No other service principal is tagged for this repository" in page and "No other warehouse is granted" in page


def test_the_identity_s_values_show_once(portal):
    link_identity_and_merge(portal)
    page = portal.client("owner").get("/apps/acme/notes").text
    card = page[page.index("<h3>Associate Azure Identity</h3>"):page.index("<h3>Enable Databricks Connection</h3>")]
    assert card.count("11111111-1111-4111-8111-111111111111") == 1 and card.count(">sp-notes<") == 2   # the facts list and the select
    assert "Linked: <span" not in card and "Not verified with Azure: checked against the" in card


def overdue_record(portal):
    """acme/notes registered with a review date that has passed, and its open review_overdue item."""
    import datetime as dt
    from portal_fakes import T0
    from portal.store.base import IntakeObserved
    portal.store.replace_intake_observed([IntakeObserved("acme/notes", "c" * 40, "intake/v2", True, portal.people["owner"].id, "low", "public",
                                                         "Stores notes.", dt.date(2026, 10, 1), None, None)], observed_at=T0)
    portal.store.open_rereview_item(repo="acme/notes", reason="review_overdue", key="2026-10-01", now=T0)


def test_an_open_review_date_shows_on_the_app_page_with_the_way_to_renew(portal):
    """Spec 2026-10-31 R7: the owner sees open date reasons as a banner on the app page, with a link to change or renew."""
    overdue_record(portal)
    owner = portal.client("owner").get("/apps/acme/notes").text
    assert ("<p class=\"callout warn\" role=\"status\">This app's registration was due for review on 2026-10-01. "
            '<a href="/register?repo=acme/notes">Change or renew the registration</a>.</p>') in owner
    admin = portal.client("admin").get("/apps/acme/notes").text   # a portal admin who isn't the repo's admin sees it, without the link
    assert "was due for review on 2026-10-01. An admin of the repository can renew it." in admin and "/register?repo=" not in admin


def test_a_registered_app_without_a_reason_still_links_to_change_or_renew(portal):
    import datetime as dt
    from portal_fakes import T0
    from portal.store.base import IntakeObserved
    portal.store.replace_intake_observed([IntakeObserved("acme/notes", "c" * 40, "intake/v2", True, portal.people["owner"].id, "low", "public",
                                                         "Stores notes.", dt.date(2027, 10, 1), None, None)], observed_at=T0)
    text = portal.client("owner").get("/apps/acme/notes").text
    assert '<p><a href="/register?repo=acme/notes">Change or renew the registration</a></p>' in text and "callout warn" not in text


def test_a_scorecard_item_is_not_the_owner_s_banner(portal):
    """Owners act through a change request; a scorecard signal is the admins' to decide (spec R7)."""
    from portal_fakes import T0
    portal.store.add_rereview_event(doc={"schema": "re-review-event/v1", "repo": "acme/notes", "pr": 7, "head_sha": "a" * 40, "tree_sha": "b" * 40,
                                         "scorecard_version": "0.5.1", "created": "2026-10-31T12:00:00Z", "level": "elevated",
                                         "floor": "standard", "complete": True, "signals": []}, run_repo="acme/scorecard-factory", run_id=1, now=T0)
    portal.store.open_rereview_item(repo="acme/notes", reason="scorecard_signal", key="a" * 40, now=T0, event=("a" * 40, ("T-17",)))
    assert "due for review" not in portal.client("owner").get("/apps/acme/notes").text


def test_an_app_registered_only_in_the_factory_says_so_not_that_it_has_no_registration(portal):
    """Visual check of 0.5.1: the review banner beside "No portal registration" read as a contradiction."""
    overdue_record(portal)
    text = portal.client("owner").get("/apps/acme/notes").text
    assert "Registered outside the portal (the factory's intake record); a change or renewal here replaces it." in text
    assert "No portal registration" not in text
