import pytest
from portal_fakes import T0, make_settings

from portal.registration import form_draft, intake_record
from portal.store.base import RepoAdmin


def submit(portal, who, **form):
    data = {"csrf_token": portal.csrfs[who], "repo": "acme/tools", "purpose": "Builds internal tools; no PHI, no email.",
            "data_classification": "public", **form}
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
    data = {"csrf_token": portal.csrfs["owner"], "purpose": "Stores short notes; no email, no files, no PHI.", "data_classification": "public"}
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


@pytest.mark.parametrize("old, new", [("internal", "confidential"), ("restricted", "confidential"), ("phi", "protected")])
def test_the_form_refuses_a_retired_classification_by_name(old, new):
    draft, errors = form_draft("acme/tools", "Builds internal tools; no email.", old)
    assert draft is None and errors == [f"`{old}` is retired: use `{new}`."]


def test_a_retired_classification_posted_to_the_form_comes_back_named(portal):
    response = submit(portal, "owner", data_classification="phi")
    assert response.status_code == 400 and "`phi` is retired: use `protected`." in response.text


def test_the_approval_refuses_a_retired_classification_by_name(portal):
    req = portal.store.get_request(portal.seeds["request"])
    record, errors = intake_record(req, approver_id=portal.people["admin"].id, tier="high", classification="restricted",
                                   review_by="2027-10-23", justification="Purpose and classification checked.", now=portal.clock(),
                                   evidence=portal.store.evidence_for(req.id, req.submission))
    assert record is None and "`restricted` is retired: use `confidential`." in errors


def test_the_form_has_no_tier_and_a_posted_tier_is_ignored(portal):
    text = portal.client("owner").get("/register").text
    assert "proposed_tier" not in text and "Proposed tier" not in text
    assert submit(portal, "owner", proposed_tier="high").status_code == 303
    req = next(r for r in portal.store.requests_by(portal.people["owner"].id) if r.repo == "acme/tools")
    assert "proposed_tier" not in req.draft


LINES = (("public", "anyone may see it"), ("confidential", "staff and the groups that need it"),
         ("protected", "regulated or highly sensitive data, named people only"))
POLICY = '<a href="https://policies.example.test/data" target="_blank" rel="noopener noreferrer">Read the data classification policy</a>'


def with_policy(portal, url="https://policies.example.test/data"):
    portal.app.state.settings = make_settings(directory=portal.settings.directory, classification_policy_url=url)
    return portal


def test_the_register_form_offers_three_cards_with_their_lines_and_the_policy_link(portal):
    text = with_policy(portal).client("owner").get("/register").text
    for level, means in LINES:
        assert f'<input type="radio" name="data_classification" value="{level}" required>' in text and means in text
    assert POLICY in text and "<legend>Data classification</legend>" in text and "<select name=\"data_classification\"" not in text


def test_the_policy_link_is_escaped_in_its_quoted_href(portal):
    text = with_policy(portal, "https://policies.example.test/data?a=1&b=2#levels").client("owner").get("/register").text
    assert '<a href="https://policies.example.test/data?a=1&amp;b=2#levels" target="_blank" rel="noopener noreferrer">' in text


def test_without_a_policy_url_the_link_is_hidden(portal):
    text = portal.client("owner").get("/register").text   # make_settings leaves it empty
    assert "Read the data classification policy" not in text and 'value="confidential" required>' in text


def test_a_refused_form_keeps_the_chosen_card(portal):
    text = submit(portal, "owner", purpose="short").text
    assert '<input type="radio" name="data_classification" value="public" required checked>' in text


def test_the_resubmit_form_uses_the_same_cards(portal):
    req = portal.store.get_request(portal.seeds["request"])
    portal.store.decide(request_id=req.id, actor_id=portal.people["admin"].id, actor_groups=["golden-road-admins"], approve=False,
                        note="Say what it does not do.", final=None, now=portal.clock())
    text = with_policy(portal).client("owner").get(f"/requests/{req.id}").text
    assert '<input type="radio" name="data_classification" value="public" required checked>' in text and POLICY in text


def test_the_register_form_has_real_labels_and_a_live_count_for_the_purpose(portal):
    text = portal.client("owner").get("/register").text
    assert '<label for="repo">Repository</label>' in text and '<select id="repo" name="repo" class="mono" required>' in text
    assert "Only repositories you administer on GitHub appear here." in text
    assert '<label for="purpose">What the app is for, and what it does not do</label>' in text
    assert 'data-counter="purpose-count" data-min="10"' in text and '<span class="counter" id="purpose-count"></span>' in text
    assert '<p id="draft-status" class="callout info" role="status" hidden></p>' in text
    assert '<button class="gold" type="submit">Submit for review</button>' in text


def test_a_change_s_record_keeps_the_scorecard_mark_of_the_record_it_replaces(portal):
    """a complete intake/v2 written from the approval would otherwise drop `scorecard: central`, and the app's PRs would go
    unreviewed in central mode."""
    from portal_fakes import give_evidence, record_view
    req = portal.store.create_registration(person_id=portal.people["owner"].id, now=T0, draft={
        "schema": "registration-draft/v1", "repo": "acme/tools", "purpose": "Builds internal tools; no PHI, no email.",
        "data_classification": "public"})
    evidence = give_evidence(portal.store, req, record_sha="c" * 40, record=record_view(portal.people["owner"].id, scorecard="central"))
    final, errors = intake_record(req, approver_id=portal.people["admin"].id, tier="moderate", classification="public", review_by="2027-10-23",
                                  justification="Change checked against the current record.", now=T0, evidence=evidence)
    assert errors == [] and final["scorecard"] == "central"
    from portal.writer.render import intake_yaml
    assert "scorecard: central" in intake_yaml(final, req.id).splitlines()


def test_a_first_registration_s_record_has_no_scorecard_mark(portal):
    req = portal.store.get_request(portal.seeds["request"])
    final, _ = intake_record(req, approver_id=portal.people["admin"].id, tier="moderate", classification="public", review_by="2027-10-23",
                             justification="Purpose and classification checked.", now=T0,
                             evidence=portal.store.evidence_for(req.id, req.submission))
    assert "scorecard" not in final


def test_an_invalid_record_s_central_mark_is_carried_by_the_approval(portal):
    from portal_fakes import give_evidence
    req = portal.store.create_registration(person_id=portal.people["owner"].id, now=T0, draft={
        "schema": "registration-draft/v1", "repo": "acme/tools", "purpose": "Builds internal tools; no PHI, no email.",
        "data_classification": "public"})
    evidence = give_evidence(portal.store, req, record_sha="c" * 40, record={"schema": "invalid", "scorecard": "central"})
    final, errors = intake_record(req, approver_id=portal.people["admin"].id, tier="moderate", classification="public", review_by="2027-10-23",
                                  justification="Change checked against the current record.", now=T0, evidence=evidence)
    assert errors == [] and final["scorecard"] == "central"
