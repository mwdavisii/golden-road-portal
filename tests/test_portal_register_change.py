"""Changing or renewing a registration (spec 2026-10-31 C1 to C9): the same form, the same floor and approval, shown against the record."""

import datetime as dt

from portal_fakes import T0, give_evidence, record_view

from portal.store.base import IntakeObserved

PURPOSE = "Builds internal tools; no PHI, no email."


def observe(portal, repo="acme/tools", **changes):
    row = dict(repo=repo, blob_sha="c" * 40, schema="intake/v2", by_portal=True, owner=portal.people["owner"].id, tier="moderate",
               data_classification="confidential", purpose=PURPOSE, review_by=dt.date(2026, 11, 15), scorecard=None, problem=None)
    portal.store.replace_intake_observed([IntakeObserved(**{**row, **changes})], observed_at=T0)


def test_a_registered_repo_s_page_is_change_or_renew_and_pre_filled_from_the_record(portal):
    observe(portal)
    text = portal.client("owner").get("/register?repo=acme/tools").text
    assert "<h1>Change or renew the registration</h1>" in text and "<title>Change or renew the registration - golden road</title>" in text
    assert f">{PURPOSE}</textarea>" in text and 'value="confidential" required checked' in text
    assert '<option value="acme/tools" selected>' in text
    assert "<div><dt>Tier</dt><dd>moderate</dd></div>" in text and "<div><dt>Review by</dt><dd>2026-11-15</dd></div>" in text


def test_the_repo_is_found_in_any_case(portal):
    observe(portal)
    assert "<h1>Change or renew the registration</h1>" in portal.client("owner").get("/register?repo=ACME/Tools").text


def test_a_repo_the_person_does_not_administer_is_ignored(portal):
    observe(portal, repo="acme/other")
    text = portal.client("owner").get("/register?repo=acme/other").text
    assert "<h1>Register an app</h1>" in text and "acme/other" not in text


def test_an_unregistered_repo_is_a_first_registration_with_it_chosen(portal):
    text = portal.client("owner").get("/register?repo=acme/tools").text
    assert "<h1>Register an app</h1>" in text and '<option value="acme/tools" selected>' in text


def test_a_v1_record_says_it_was_registered_outside_the_portal(portal):
    observe(portal, schema="intake/v1", by_portal=False, owner="platform-team", review_by=None)
    text = portal.client("owner").get("/register?repo=acme/tools").text
    assert "<div><dt>Review by</dt><dd>none (registered outside the portal)</dd></div>" in text


def test_a_record_that_can_t_be_read_shows_the_change_page_without_pre_filling(portal):
    observe(portal, schema=None, by_portal=False, owner=None, tier=None, data_classification=None, purpose=None, review_by=None, problem="invalid")
    text = portal.client("owner").get("/register?repo=acme/tools").text
    assert "<h1>Change or renew the registration</h1>" in text and "can't be read" in text and PURPOSE not in text


def test_a_change_is_submitted_as_a_registration_request(portal):
    observe(portal)
    response = portal.client("owner").post("/register", data={"csrf_token": portal.csrfs["owner"], "repo": "acme/tools", "purpose": PURPOSE,
                                                               "data_classification": "confidential"})
    assert response.status_code == 303
    [req] = [r for r in portal.store.requests_by(portal.people["owner"].id) if r.repo == "acme/tools"]
    assert (req.kind, req.state, req.draft["purpose"]) == ("registration", "pending", PURPOSE)


def test_one_open_request_per_repo_whether_first_or_change(portal):
    observe(portal, repo="acme/notes")   # acme/notes already has the seeded pending request
    response = portal.client("owner").post("/register", data={"csrf_token": portal.csrfs["owner"], "repo": "acme/notes", "purpose": PURPOSE,
                                                               "data_classification": "public"})
    assert response.status_code == 409 and "That repository already has an open registration." in response.text
    assert "<h1>Change or renew the registration</h1>" in response.text


def pending_change(portal, record, purpose=PURPOSE, classification="public", **floor):
    req = portal.store.create_registration(person_id=portal.people["owner"].id, now=T0, draft={
        "schema": "registration-draft/v1", "repo": "acme/tools", "purpose": purpose, "data_classification": classification})
    give_evidence(portal.store, req, record_sha="c" * 40, record=record, **floor)
    return req


def tools_section(text):
    start = text.index('<h2 class="repo">acme/tools</h2>')
    return text[start:text.find("<section>", start)]


def v1_record():
    record = record_view("platform-team", schema="intake/v1", by_portal=False, tier="high", data_classification="confidential",
                         purpose="A notes API.")
    del record["review_by"]
    return record


def test_the_approval_shows_current_and_proposed_for_each_field(portal):
    pending_change(portal, v1_record())
    section = tools_section(portal.client("admin").get("/admin/approvals").text)
    assert '<span class="tag">change</span>' in section and "A change to the current registration" in section
    assert "The current record is intake/v1, registered outside the portal." in section
    assert '<tr class="mismatch"><th scope="row">Record</th><td>intake/v1</td><td>intake/v2</td></tr>' in section
    assert '<p class="callout warn">Owner changes from platform-team to Owner.</p>' in section
    assert '<tr class="mismatch"><th scope="row">Data classification</th><td>confidential</td><td>public (claimed; an admin decides)</td></tr>' in section
    assert '<tr><th scope="row">Tier</th><td>high</td><td>an admin decides</td></tr>' in section
    assert '<tr><th scope="row">Review by</th><td>none (intake/v1)</td><td>an admin decides</td></tr>' in section
    assert "<h4>Current purpose</h4>" in section and "A notes API." in section


def test_a_renewal_is_labelled_and_names_no_owner_change(portal):
    pending_change(portal, record_view(portal.people["owner"].id))
    section = tools_section(portal.client("admin").get("/admin/approvals").text)
    assert "A renewal of the current registration" in section and "Owner changes" not in section
    assert "The current record is intake/v2, written by the portal." in section and "<h4>Current purpose</h4>" not in section


def test_a_hand_written_record_and_a_kept_scorecard_mark_say_so(portal):
    pending_change(portal, record_view(portal.people["owner"].id, by_portal=False, scorecard="central"))
    section = tools_section(portal.client("admin").get("/admin/approvals").text)
    assert "The current record is intake/v2, written by hand." in section
    assert '<tr><th scope="row">Scorecard</th><td>central</td><td>central (kept)</td></tr>' in section


def test_a_record_that_is_not_valid_is_shown_as_such(portal):
    pending_change(portal, {"schema": "invalid"})
    section = tools_section(portal.client("admin").get("/admin/approvals").text)
    assert "The current record is not a valid intake record: approving replaces it with a complete intake/v2 record." in section


def test_the_requester_sees_the_same_view_on_the_request_page(portal):
    req = pending_change(portal, record_view(portal.people["owner"].id))
    assert "A renewal of the current registration" in portal.client("owner").get(f"/requests/{req.id}").text


def test_a_first_registration_shows_no_change_view(portal):
    text = portal.client("owner").get(f"/requests/{portal.seeds['request']}").text
    assert "current registration" not in text and "Owner changes" not in text


def test_a_change_below_the_floor_still_needs_the_override(portal):
    req = pending_change(portal, record_view(portal.people["owner"].id), floor_tier="high", floor_classification="confidential")
    form = {"csrf_token": portal.csrfs["admin"], "tier": "low", "data_classification": "public", "review_by": "2027-10-23",
            "justification": "Change checked against the record.", "submission": "1"}
    refused = portal.client("admin").post(f"/admin/requests/{req.id}/approve", data=form)
    assert refused.status_code == 400 and "below the platform&#39;s floor" in refused.text
    ok = portal.client("admin").post(f"/admin/requests/{req.id}/approve",
                                     data={**form, "floor_override_reason": "Internal tool only; security agreed in the review meeting."})
    assert ok.status_code == 303 and portal.store.get_request(req.id).final["floor_override"]["by"] == portal.people["admin"].id


def test_an_invalid_record_s_kept_central_mark_is_said(portal):
    pending_change(portal, {"schema": "invalid", "scorecard": "central"})
    section = tools_section(portal.client("admin").get("/admin/approvals").text)
    assert "The record says <span class=\"mono\">scorecard: central</span>, and the replacement keeps it." in section


# ---- more cases ----

def test_a_record_under_two_spellings_shows_the_change_card_with_the_problem_and_no_pre_fill(portal):
    observe(portal, schema=None, by_portal=False, owner=None, tier=None, data_classification=None, purpose=None, review_by=None,
            problem="case_mismatch")
    text = portal.client("owner").get("/register?repo=acme/tools").text
    assert "<h1>Change or renew the registration</h1>" in text and PURPOSE not in text
    assert "The factory holds this repository's record under two spellings of its name" in text


def test_signed_out_the_sign_in_link_comes_back_to_the_same_repo(portal):
    observe(portal)
    text = portal.client().get("/register?repo=acme/tools").text
    assert 'href="/login?next=/register%3Frepo%3Dacme/tools"' in text and "Change or renew" not in text and PURPOSE not in text
    from portal.web.routes_auth import safe_next
    assert safe_next("/register?repo=acme/tools") == "/register?repo=acme/tools" and safe_next("//evil.example.test/x") == "/"


def test_a_hostile_record_is_shown_as_text_everywhere(portal):
    hostile = "Builds tools. </textarea><script>alert(1)</script>"
    observe(portal, purpose=hostile)
    text = portal.client("owner").get("/register?repo=acme/tools").text
    assert "<script>alert(1)</script>" not in text and "&lt;/textarea&gt;&lt;script&gt;" in text
    req = pending_change(portal, record_view("<b>team</b>", schema="intake/v1", by_portal=False, purpose=hostile))
    for who, path in (("admin", "/admin/approvals"), ("owner", f"/requests/{req.id}")):
        page_text = portal.client(who).get(path).text
        assert "<b>team</b>" not in page_text and "&lt;b&gt;team&lt;/b&gt;" in page_text, path
        assert "<script>alert(1)</script>" not in page_text, path


def test_someone_neither_the_requester_nor_an_admin_can_t_see_a_change(portal):
    req = pending_change(portal, record_view(portal.people["owner"].id))
    assert portal.client("other").get(f"/requests/{req.id}").status_code == 404
