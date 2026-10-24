import html

import pytest
from portal_fakes import T0, evidence_doc, give_evidence

from portal.registration import NO_EVIDENCE
from portal.store.base import RepoAdmin

LONG = "The spec over-declares egress; checked with the team."


def fresh(portal, repo="acme/tools", *, floor=None, doc=None, claim=("low", "internal")):
    """A second pending registration (the fixture's acme/notes already has evidence at the floor low/internal). `floor` gives it evidence."""
    portal.store.replace_repo_admins([RepoAdmin("acme/notes", "owner-gh", 1001, T0), RepoAdmin(repo, "owner-gh", 1001, T0)], observed_at=T0)
    req = portal.store.create_registration(person_id=portal.people["owner"].id, now=T0, draft={
        "schema": "registration-draft/v1", "repo": repo, "purpose": "Builds internal tools; no PHI, no email.",
        "data_classification": claim[1], "proposed_tier": claim[0]})
    if floor:
        give_evidence(portal.store, req, floor_tier=floor[0], floor_classification=floor[1], **(doc or {}))
    return req


def approve(portal, req, **form):
    data = {"csrf_token": portal.csrfs["admin"], "submission": str(req.submission), "tier": "moderate", "data_classification": "internal",
            "review_by": "2027-10-23", "justification": "Purpose and classification checked.", **form}
    return portal.client("admin").post(f"/admin/requests/{req.id}/approve", data=data)


def test_the_approval_page_shows_claimed_computed_and_effective_with_mismatches(portal):
    fresh(portal, floor=("moderate", "internal"), doc={"signals": [{"rule": "write_roles", "line": "Write roles: editor."}],
                                                       "write_roles": ["editor"]})
    text = portal.client("admin").get("/admin/approvals").text
    assert text.count('<tr class="mismatch">') == 1 and "tier</th>" in text.lower()
    assert "Write roles: editor." in text and "e" * 40 in text
    tier_select = text[text.index('<h2 class="repo">acme/tools'):]
    tier_select = tier_select[tier_select.index('<select name="tier">'):]
    assert '<option value="moderate" selected>' in tier_select.split("</select>")[0]


def test_approve_is_disabled_and_refused_without_evidence(portal):
    req = fresh(portal)
    text = portal.client("admin").get("/admin/approvals").text
    assert "Waiting for the platform's evidence" in text and "disabled" in text
    response = approve(portal, req)
    assert response.status_code == 409 and NO_EVIDENCE in html.unescape(response.text)
    assert portal.store.get_request(req.id).state == "pending"


@pytest.mark.parametrize("problem, sentence", [
    ("rules_missing", "the factory has no config/registration.yaml on its default branch"),
    ("rules_invalid", "the factory's config/registration.yaml is not valid"),
    ("rules_unreadable", "the factory's config/registration.yaml can't be read"),
    ("repo_unreadable", "the portal's read App can't read this repository"),
])
def test_the_page_says_why_the_evidence_is_waiting(portal, problem, sentence):
    req = fresh(portal)
    portal.store.add_evidence_problem(request_id=req.id, submission=1, problem=problem, now=T0)
    assert sentence in html.unescape(portal.client("admin").get("/admin/approvals").text)


def test_approving_below_the_floor_needs_a_30_character_override_reason(portal):
    req = fresh(portal, floor=("moderate", "internal"))
    assert approve(portal, req, tier="low").status_code == 400
    assert approve(portal, req, tier="low", floor_override_reason="x" * 29).status_code == 400
    assert portal.store.get_request(req.id).state == "pending"
    assert approve(portal, req, tier="low", floor_override_reason=LONG).status_code == 303
    final = portal.store.get_request(req.id).final
    assert final["floor_override"]["by"] == portal.people["admin"].id and final["floor_override"]["reason"] == LONG
    assert final["tier"] == "low" and final["floor_tier"] == "moderate"


def test_an_override_goes_into_the_audit_log(portal):
    req = fresh(portal, floor=("moderate", "internal"))
    assert approve(portal, req, tier="low", floor_override_reason=LONG).status_code == 303
    entry = next(e for e in portal.store.audit_entries() if e.action == "request.approved" and e.subject_id == req.id)
    assert entry.after["floor_override"]["reason"] == LONG
    assert portal.store.get_request(req.id).floor_override["by"] == portal.people["admin"].id


@pytest.mark.parametrize("tier", ["moderate", "high"])
def test_approving_at_or_above_the_floor_records_no_override(portal, tier):
    req = fresh(portal, floor=("moderate", "internal"))
    assert approve(portal, req, tier=tier, floor_override_reason="Typed anyway, though nothing here is below the floor.").status_code == 303
    got = portal.store.get_request(req.id)
    assert "floor_override" not in got.final and got.floor_override is None


def test_the_evidence_view_escapes_everything(portal):
    req = fresh(portal, floor=("low", "internal"))
    stored = portal.store._evidence[(req.id, 1)]
    bad = {**stored.evidence, "signals": [{"rule": "write_roles", "line": "<script>alert(1)</script>"}],
           "egress": [{"host": "<b>", "port": 443, "model_gateway": False}], "builder_version": "<i>x</i>"}
    portal.store._evidence[(req.id, 1)] = type(stored)(**{**stored.__dict__, "evidence": bad})  # bypasses the validation the collector does
    for who, url in (("admin", "/admin/approvals"), ("owner", f"/requests/{req.id}")):
        text = portal.client(who).get(url).text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text and "<script>alert" not in text and "<b>:443" not in text and "<i>x" not in text


def test_the_approve_form_carries_the_submission_it_was_loaded_for(portal):
    req = fresh(portal, floor=("low", "internal"))
    text = portal.client("admin").get("/admin/approvals").text
    assert f'<form method="post" action="/admin/requests/{req.id}/approve">' in text
    assert text[text.index(f"/admin/requests/{req.id}/approve"):].split("</form>")[0].count('name="submission" value="1"') == 1


def test_an_approval_from_a_page_loaded_before_a_resubmission_is_refused(portal):
    """The admin loads the page (submission 1), the request is sent back and resubmitted (submission 2, with its own evidence), and
    the admin's form arrives: it was judged against what the page showed, so it is refused."""
    req = fresh(portal, floor=("low", "internal"))
    portal.store.decide(request_id=req.id, actor_id=portal.people["ops"].id, actor_groups=["g"], approve=False, note="Say what it stores.",
                        final=None, now=T0)
    again = portal.store.resubmit(request_id=req.id, person_id=portal.people["owner"].id, draft=portal.store.get_request(req.id).draft, now=T0)
    assert again.submission == 2
    give_evidence(portal.store, again)
    stale = approve(portal, req, submission="1")
    assert stale.status_code == 409 and "changed since you loaded this page" in html.unescape(stale.text)
    assert portal.store.get_request(req.id).state == "pending"
    assert approve(portal, req, submission="2").status_code == 303


@pytest.mark.parametrize("value", ["", "x", "0", "1.5"])
def test_an_approval_without_a_usable_submission_is_a_400(portal, value):
    req = fresh(portal, floor=("low", "internal"))
    assert approve(portal, req, submission=value).status_code == 400 and portal.store.get_request(req.id).state == "pending"


def test_an_empty_admin_post_is_still_400(portal):
    for req in (portal.store.get_request(portal.seeds["request"]), fresh(portal)):  # with evidence and without
        response = portal.client("admin").post(f"/admin/requests/{req.id}/approve", data={"csrf_token": portal.csrfs["admin"]})
        assert response.status_code == 400


# ---- the requester's page ----


def test_the_requester_sees_the_evidence_floor_and_effective_values(portal):
    req = fresh(portal, floor=("moderate", "internal"))
    text = portal.client("owner").get(f"/requests/{req.id}").text
    assert "Computed by the platform" in text and '<tr class="mismatch">' in text and "raised by the floor" in text
    assert "Your proposed tier" in text


def test_the_requester_is_told_when_the_evidence_is_still_waiting(portal):
    req = fresh(portal)
    assert "Waiting for the platform's evidence" in portal.client("owner").get(f"/requests/{req.id}").text


def test_the_claims_are_stored_as_submitted(portal):
    req = fresh(portal, floor=("high", "restricted"))
    assert portal.store.get_request(req.id).draft["proposed_tier"] == "low"
    assert portal.store.get_request(req.id).draft["data_classification"] == "internal"


def test_another_contributor_never_sees_the_evidence(portal):
    req = fresh(portal, floor=("moderate", "internal"))
    assert portal.client("other").get(f"/requests/{req.id}").status_code == 404


def test_the_register_page_says_the_platform_computes_a_minimum(portal):
    text = portal.client("owner").get("/register").text
    assert "The platform computes a minimum tier and classification from the repo" in text


def test_after_approval_the_request_page_shows_what_was_approved(portal):
    req = fresh(portal, floor=("low", "internal"))
    before = portal.client("owner").get(f"/requests/{req.id}").text
    assert "Approved tier" not in before
    assert approve(portal, req, tier="high", data_classification="restricted").status_code == 303
    text = portal.client("owner").get(f"/requests/{req.id}").text
    assert "<dt>Approved tier</dt><dd>high</dd>" in text and "<dt>Approved data classification</dt><dd>restricted</dd>" in text
    # the claim and the floor are still shown as they were
    assert "Your proposed tier" in text and "Computed by the platform" in text


def test_a_request_approved_below_the_floor_says_so(portal):
    req = fresh(portal, floor=("moderate", "internal"))
    assert approve(portal, req, tier="low", floor_override_reason=LONG).status_code == 303
    text = portal.client("owner").get(f"/requests/{req.id}").text
    assert "<dt>Approved tier</dt><dd>low</dd>" in text and "below the platform's floor" in html.unescape(text)
