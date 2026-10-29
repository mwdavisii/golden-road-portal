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
    assert response.status_code == 303 and response.headers["location"] == "/apps/acme/notes"
    [req] = bindings(portal)
    assert req.state == "approved" and req.final["azure_identity"]["directory_id"] == "sp-notes" and req.final["repo"] == "acme/notes"
    assert req.requested_by == portal.people["owner"].id and req.decided_by is None
    entry = next(e for e in portal.store.audit_entries() if e.subject_id == req.id and e.action == "request.created")
    assert entry.actor_id == portal.people["owner"].id and entry.before == {"binding": None}
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "pending" in page and "AZURE_CLIENT_ID" in page and "11111111-1111-4111-8111-111111111111" in page
    assert "login.microsoftonline.com" in page and "once the pending change is recorded" in page


def test_the_url_s_case_resolves_to_the_stored_name(portal):
    assert post(portal, "owner", "/apps/ACME/Notes/azure-identity", principal_id="sp-notes").headers["location"] == "/apps/acme/notes"
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
        assert response.status_code == 303 and response.headers["location"] == "/apps/acme/notes"
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
