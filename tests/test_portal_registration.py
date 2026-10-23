from portal_fakes import T0

from portal.store.base import RepoAdmin


def submit(portal, who, **form):
    data = {"csrf_token": portal.csrfs[who], "repo": "acme/tools", "purpose": "Builds internal tools; no PHI, no email.",
            "data_classification": "internal", "proposed_tier": "low", **form}
    return portal.client(who).post("/register", data=data)


def test_signed_out_the_page_keeps_the_draft_for_after_sign_in_and_shows_no_form(portal):
    text = portal.client().get("/register").text
    assert 'src="/static/register.js"' in text and 'id="register-form"' not in text and "/login?next=/register" in text


def test_signed_in_the_form_lists_only_the_person_s_admin_repos(portal):
    text = portal.client("owner").get("/register").text
    assert 'value="acme/notes"' in text and 'value="acme/tools"' in text and 'id="draft-status"' in text
    assert "Link your GitHub login" in portal.client("other").get("/register").text  # no link, so no repos


def test_only_a_repo_admin_can_register(portal):
    portal.store.add_link(person_id=portal.people["other"].id, github_login="other-gh", github_user_id=2002, source="admin",
                          actor_id=portal.people["admin"].id, reason="seeded for this test", now=T0)
    response = submit(portal, "other", repo="acme/tools")
    assert response.status_code == 400 and "not an admin" in response.text
    portal.store.replace_repo_admins([RepoAdmin("acme/notes", "owner-gh", 1001, T0), RepoAdmin("acme/tools", "other-gh", 2002, T0)], observed_at=T0)
    assert submit(portal, "other", repo="acme/tools").status_code == 303


def test_an_admin_made_link_without_a_user_id_matches_by_login(portal):
    portal.store.add_link(person_id=portal.people["other"].id, github_login="Other-GH", github_user_id=None, source="admin",
                          actor_id=portal.people["admin"].id, reason="seeded for this test", now=T0)
    portal.store.replace_repo_admins([RepoAdmin("acme/tools", "other-gh", 2002, T0)], observed_at=T0)
    assert 'value="acme/tools"' in portal.client("other").get("/register").text
    assert submit(portal, "other", repo="acme/tools").status_code == 303


def test_a_registration_is_pending_and_writes_nothing(portal):
    response = submit(portal, "owner")
    assert response.status_code == 303
    req = portal.store.get_request(response.headers["location"].rsplit("/", 1)[1])
    assert (req.state, req.repo, req.requested_by) == ("pending", "acme/tools", portal.people["owner"].id)
    assert portal.fakes["github"].calls == [], "nothing reaches git before an admin approves"


def test_a_second_open_registration_of_a_repo_is_refused(portal):
    assert submit(portal, "owner", repo="acme/notes").status_code == 409


def test_bad_form_input_is_shown_escaped(portal):
    response = submit(portal, "owner", purpose="<script>x</script> stores things", data_classification="secret")  # invalid, so the form comes back with the text
    assert response.status_code == 400 and "<script>x</script>" not in response.text and "&lt;script&gt;" in response.text


def test_the_request_page_shows_the_purpose_as_text(portal):
    portal.store.decide(request_id=portal.seeds["request"], actor_id=portal.people["admin"].id, actor_groups=[], approve=False,
                        note="<b>Say</b> what it does not do.", final=None, now=T0)
    text = portal.client("owner").get(f"/requests/{portal.seeds['request']}").text
    assert "&lt;b&gt;Say&lt;/b&gt;" in text and 'action="/requests/' in text  # sent back: the resubmit form


def test_a_sent_back_request_is_resubmitted_by_its_requester(portal):
    rid = portal.seeds["request"]
    portal.store.decide(request_id=rid, actor_id=portal.people["admin"].id, actor_groups=[], approve=False, note="Say more.", final=None, now=T0)
    data = {"csrf_token": portal.csrfs["owner"], "purpose": "Stores short notes; no email, no files, no PHI.", "data_classification": "internal",
            "proposed_tier": "low"}
    assert portal.client("owner").post(f"/requests/{rid}", data=data).status_code == 303
    assert portal.store.get_request(rid).state == "pending"


def test_an_old_repo_admin_observation_refuses_registering_and_says_why(portal):
    portal.clock.advance(minutes=11)  # past the default limit of two collector intervals
    page = portal.client("owner").get("/register").text
    assert "too old" in page and 'value="acme/tools"' not in page
    response = submit(portal, "owner", repo="acme/tools")
    assert response.status_code == 503 and "too old" in response.text and "collector" in response.text
    assert [r.repo for r in portal.store.requests_by(portal.people["owner"].id)] == ["acme/notes"], "nothing was registered"


def test_a_fresh_observation_is_enough_again_after_the_collector_runs(portal):
    portal.clock.advance(minutes=11)
    assert submit(portal, "owner", repo="acme/tools").status_code == 503
    now = portal.clock()
    portal.store.replace_repo_admins([RepoAdmin("acme/notes", "owner-gh", 1001, now), RepoAdmin("acme/tools", "owner-gh", 1001, now)], observed_at=now)
    assert submit(portal, "owner", repo="acme/tools").status_code == 303
