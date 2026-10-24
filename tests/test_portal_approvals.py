from portal_fakes import T0

from portal.contracts import validate
from portal.store.base import RepoAdmin


def approve(portal, who="admin", **form):
    data = {"csrf_token": portal.csrfs[who], "submission": "1", "tier": "moderate", "data_classification": "internal", "review_by": "2027-10-23",
            "justification": "Purpose and classification checked.", **form}
    return portal.client(who).post(f"/admin/requests/{portal.seeds['request']}/approve", data=data)


def test_the_approval_page_shows_the_request_with_its_context_escaped(portal):
    text = portal.client("admin").get("/admin/approvals").text
    assert "acme/notes" in text and "owner-gh" in text and "Admin of acme/notes" in text
    assert 'name="review_by" value="2027-10-23"' in text  # 12 months, the default setting


def test_the_approval_page_escapes_the_purpose(portal):
    portal.store.replace_repo_admins([RepoAdmin("acme/notes", "owner-gh", 1001, T0), RepoAdmin("acme/evil", "owner-gh", 1001, T0)], observed_at=T0)
    portal.store.create_registration(person_id=portal.people["owner"].id, now=T0, draft={
        "schema": "registration-draft/v1", "repo": "acme/evil", "purpose": "<script>alert(1)</script> and more words",
        "data_classification": "internal", "proposed_tier": "low"})
    text = portal.client("admin").get("/admin/approvals").text
    assert "<script>alert(1)" not in text and "&lt;script&gt;alert(1)&lt;/script&gt;" in text


def test_an_approval_stores_the_intake_v2_record_and_the_approver_s_groups(portal):
    assert approve(portal).status_code == 303
    req = portal.store.get_request(portal.seeds["request"])
    assert req.state == "approved" and req.decision_groups == ("golden-road-admins",) and validate("intake.v2", req.final) == []
    assert (req.final["approved_by"], req.final["owner"], req.final["tier"]) == (portal.people["admin"].id, portal.people["owner"].id, "moderate")
    entry = portal.store.audit_entries()[0]
    assert entry.action == "request.approved" and entry.reason == "Purpose and classification checked."


def test_a_requester_can_never_approve_their_own_request(portal):
    admin = portal.people["admin"]
    portal.store.replace_repo_admins([RepoAdmin("acme/self", "admin-gh", 3003, T0)], observed_at=T0)
    portal.store.add_link(person_id=admin.id, github_login="admin-gh", github_user_id=3003, source="admin", actor_id=portal.people["ops"].id,
                          reason="seeded for this test", now=T0)
    mine = portal.store.create_registration(person_id=admin.id, now=T0, draft={"schema": "registration-draft/v1", "repo": "acme/self",
                                            "purpose": "An admin's own app.", "data_classification": "internal", "proposed_tier": "low"})
    data = {"csrf_token": portal.csrfs["admin"], "tier": "low", "data_classification": "internal", "review_by": "2027-10-23",
            "justification": "Approving my own request."}
    response = portal.client("admin").post(f"/admin/requests/{mine.id}/approve", data=data)
    assert response.status_code == 403 and portal.store.get_request(mine.id).state == "pending"
    assert portal.client("admin").post(f"/admin/requests/{mine.id}/send-back", data={
        "csrf_token": portal.csrfs["admin"], "justification": "Sending back my own request."}).status_code == 403
    assert "another admin must decide it" in portal.client("admin").get("/admin/approvals").text


def test_approval_refuses_a_requester_who_is_no_longer_a_repo_admin(portal):
    portal.store.replace_repo_admins([], observed_at=T0)
    response = approve(portal)
    assert response.status_code == 409 and portal.store.get_request(portal.seeds["request"]).state == "pending"


def test_bad_approval_input_changes_nothing(portal):
    assert approve(portal, justification="ok").status_code == 400
    assert approve(portal, review_by="2026-10-01").status_code == 400
    assert portal.store.get_request(portal.seeds["request"]).state == "pending"


def test_sending_back_needs_a_reason_and_is_audited(portal):
    url = f"/admin/requests/{portal.seeds['request']}/send-back"
    assert portal.client("admin").post(url, data={"csrf_token": portal.csrfs["admin"], "justification": "no"}).status_code == 400
    assert portal.client("admin").post(url, data={"csrf_token": portal.csrfs["admin"], "justification": "Say what it does not do."}).status_code == 303
    assert portal.store.get_request(portal.seeds["request"]).state == "sent_back"


def test_a_decided_request_cannot_be_decided_again(portal):
    approve(portal)
    assert approve(portal).status_code == 409


def test_only_an_admin_can_approve_even_with_a_valid_csrf_token(portal):
    for who in ("owner", "other", "ops"):
        assert approve(portal, who).status_code == 403
    assert portal.store.get_request(portal.seeds["request"]).state == "pending"


def test_approval_is_refused_while_the_repo_admin_observation_is_too_old_and_the_page_says_why(portal):
    portal.clock.advance(minutes=11)
    response = approve(portal)
    assert response.status_code == 503 and "too old" in response.text
    assert portal.store.get_request(portal.seeds["request"]).state == "pending"
    page = portal.client("admin").get("/admin/approvals").text
    assert "too old" in page and "Admin of acme/notes" not in page


def test_approval_works_again_after_a_fresh_observation(portal):
    portal.clock.advance(minutes=11)
    now = portal.clock()
    portal.store.replace_repo_admins([RepoAdmin("acme/notes", "owner-gh", 1001, now)], observed_at=now)
    assert approve(portal).status_code == 303


def test_a_registration_through_a_link_without_a_verified_user_id_is_shown_as_unverified(portal):
    page = portal.client("admin").get("/admin/approvals").text
    assert "Unverified" not in page and "[admin]" in page  # the seeded link has user id 1001
    portal.store.remove_link(link_id=portal.seeds["link"], actor_id=portal.people["admin"].id, reason="replacing it for this test", now=T0)
    portal.store.add_link(person_id=portal.people["owner"].id, github_login="owner-gh", github_user_id=None, source="admin",
                          actor_id=portal.people["ops"].id, reason="no user id known for this one", now=T0)
    page = portal.client("admin").get("/admin/approvals").text
    assert "Unverified" in page and "no GitHub user ID" in page
    assert approve(portal).status_code == 303, "shown as unverified, still the approver's call"
