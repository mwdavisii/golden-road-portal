import pytest

from portal.contracts import validate


def post(portal, path, **data):
    return portal.client("admin").post(path, data={"csrf_token": portal.csrfs["admin"], **data})


def test_an_admin_adds_a_link_with_a_reason_and_it_is_audited(portal):
    person = portal.people["other"].id
    assert post(portal, f"/admin/people/{person}/links", github_login="other-gh", reason="short").status_code == 400
    assert post(portal, f"/admin/people/{person}/links", github_login="bad login", reason="confirmed by the team lead").status_code == 400
    response = post(portal, f"/admin/people/{person}/links", github_login="other-gh", reason="confirmed by the team lead")
    assert response.status_code == 303
    [link] = portal.store.links_for(person)
    assert (link.source, link.created_by) == ("admin", portal.people["admin"].id)
    entry = portal.store.audit_entries()[0]
    assert (entry.action, entry.actor_id, entry.reason) == ("identity_link.created", portal.people["admin"].id, "confirmed by the team lead")


def test_a_taken_login_is_refused(portal):
    response = post(portal, f"/admin/people/{portal.people['other'].id}/links", github_login="OWNER-gh", reason="confirmed by the team lead")
    assert response.status_code == 409


def test_an_admin_removes_a_link_with_a_reason(portal):
    assert post(portal, f"/admin/links/{portal.seeds['link']}/remove", reason="").status_code == 400
    assert post(portal, f"/admin/links/{portal.seeds['link']}/remove", reason="linked to the wrong person").status_code == 303
    assert portal.store.links_for(portal.people["owner"].id) == []
    assert post(portal, f"/admin/links/{portal.seeds['link']}/remove", reason="linked to the wrong person").status_code == 404


def test_the_audit_page_escapes_what_people_wrote(portal):
    post(portal, f"/admin/people/{portal.people['other'].id}/links", github_login="other-gh", reason="<script>alert(1)</script> checked")
    text = portal.client("admin").get("/admin/audit").text
    assert "<script>alert(1)" not in text and "&lt;script&gt;alert(1)&lt;/script&gt; checked" in text


def test_the_people_page_escapes_display_names(portal):
    portal.store.sign_in(issuer="i", subject="x", display_name="<img src=x onerror=alert(1)>", email=None, groups=[], roles=[],
                         now=portal.clock(), max_age=portal.settings.session_max)
    text = portal.client("admin").get("/admin/people").text
    assert "<img src=x" not in text and "&lt;img src=x" in text


def test_the_identity_link_export_is_identity_link_v1(portal):
    docs = portal.client("admin").get("/admin/identity-links.json").json()
    assert docs and all(validate("identity-link.v1", d) == [] for d in docs)
    assert docs[0]["github_login"] == "owner-gh" and docs[0]["created_at"].endswith("Z")


def test_an_admin_cannot_link_a_login_to_their_own_person(portal):
    response = post(portal, f"/admin/people/{portal.people['admin'].id}/links", github_login="admin-gh", reason="confirmed by myself, apparently")
    assert response.status_code == 403 and "another admin" in response.text
    assert portal.store.links_for(portal.people["admin"].id) == []


def test_an_admin_link_takes_the_numeric_user_id_from_what_the_collector_saw(portal):
    from portal.store.base import RepoAdmin
    portal.store.replace_repo_admins([RepoAdmin("acme/notes", "Other-GH", 2002, portal.clock()), RepoAdmin("acme/tools", "Other-GH", 2002, portal.clock())],
                                     observed_at=portal.clock())
    person = portal.people["other"].id
    assert post(portal, f"/admin/people/{person}/links", github_login="other-gh", reason="confirmed by the team lead").status_code == 303
    [link] = portal.store.links_for(person)
    assert (link.source, link.github_user_id) == ("admin", 2002)


def test_an_admin_link_for_a_login_the_collector_has_not_seen_has_no_user_id(portal):
    person = portal.people["other"].id
    assert post(portal, f"/admin/people/{person}/links", github_login="unseen-gh", reason="confirmed by the team lead").status_code == 303
    assert portal.store.links_for(person)[0].github_user_id is None


def test_a_login_with_two_user_ids_in_the_observation_gets_none(portal):
    from portal.store.base import RepoAdmin
    portal.store.replace_repo_admins([RepoAdmin("acme/notes", "dup-gh", 1, portal.clock()), RepoAdmin("acme/tools", "dup-gh", 2, portal.clock())],
                                     observed_at=portal.clock())
    person = portal.people["other"].id
    assert post(portal, f"/admin/people/{person}/links", github_login="dup-gh", reason="confirmed by the team lead").status_code == 303
    assert portal.store.links_for(person)[0].github_user_id is None


# ---- the reskin (spec 2026-10-30 R1): cards, real labels, alerts, toasts ----

def test_the_people_page_is_cards_with_labelled_fields(portal):
    text = portal.client("admin").get("/admin/people").text
    assert text.count('<section class="card person">') == len(portal.store.list_people())
    link = portal.seeds["link"]
    assert f'<label class="visually-hidden" for="remove-{link}">Why remove owner-gh</label>' in text
    assert '<ul class="link-list">' in text and '<form class="add-link"' in text and "No GitHub login linked." in text
    for person in portal.store.list_people():
        assert f'<label for="login-{person.id}">GitHub login</label>' in text and f'<label for="why-{person.id}">' in text


def test_a_refused_link_is_an_alert_and_a_done_one_a_toast(portal):
    response = post(portal, f"/admin/people/{portal.people['other'].id}/links", github_login="other-gh", reason="short")
    assert '<p class="error" role="alert">Give a reason of 10 to 1000 characters on one line.</p>' in response.text
    client = portal.client("admin")
    added = client.post(f"/admin/people/{portal.people['other'].id}/links",
                        data={"csrf_token": portal.csrfs["admin"], "github_login": "other-gh", "reason": "confirmed by the team lead"})
    assert added.status_code == 303 and '<p class="toast" role="status">GitHub login linked</p>' in client.get("/admin/people").text
    removed = client.post(f"/admin/links/{portal.seeds['link']}/remove", data={"csrf_token": portal.csrfs["admin"], "reason": "linked to the wrong person"})
    assert removed.status_code == 303 and '<p class="toast" role="status">GitHub login unlinked</p>' in client.get("/admin/people").text


@pytest.mark.parametrize("path", ["/admin/audit", "/admin/directory"])
def test_the_admin_tables_have_headers_with_scope(portal, path):
    text = portal.client("admin").get(path).text
    assert "<thead><tr>" in text and '<th scope="col">' in text and "<tr><th>" not in text


def test_the_audit_log_rows_are_compact_with_the_details_folded(portal):
    post(portal, f"/admin/people/{portal.people['other'].id}/links", github_login="other-gh", reason="confirmed by the team lead")
    text = portal.client("admin").get("/admin/audit").text
    table = text[text.index('<table class="audit">'):text.index("</table>")]
    assert '<colgroup><col class="when"><col class="who"><col class="action"><col class="subject"><col class="details"></colgroup>' in table
    rows = table.split("<tbody>")[1].split("</tr>")[:-1]
    assert rows and all(row.count("<details>") == 1 for row in rows)
    for row in rows:   # before, after and the reason sit inside the fold, never in a cell of their own
        assert "&#34;" not in row.split("<details>")[0] and "{" not in row.split("<details>")[0]
    assert "<summary>confirmed by the team lead</summary>" in table and '<pre class="json">' in table


def test_an_audit_action_breaks_only_after_its_dots(portal):
    text = portal.client("admin").get("/admin/audit").text
    assert "<code>identity_link.<wbr>created</code>" in text or "<code>request.<wbr>created</code>" in text
