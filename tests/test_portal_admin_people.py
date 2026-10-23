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
