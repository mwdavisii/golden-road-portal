"""One set of cases for every Store (decision D3). Task 17 runs it on Postgres too."""

import datetime as dt

import pytest
from portal_fakes import ISSUER, T0

from portal.store.base import Conflict, LoginState

HOUR, DAY = dt.timedelta(hours=1), dt.timedelta(hours=8)


def sign_in(store, subject="sub-1", roles=(), groups=(), now=T0, name="Pat"):
    return store.sign_in(issuer=ISSUER, subject=subject, display_name=name, email=f"{subject}@example.test",
                         groups=list(groups), roles=list(roles), now=now, max_age=DAY)


def test_first_sign_in_creates_a_person_and_audits_it(store):
    person, token, csrf = sign_in(store)
    assert person.id.startswith("p_") and token and csrf and token != csrf
    assert store.get_person(person.id) == person
    [entry] = store.audit_entries()
    assert (entry.action, entry.subject_id, entry.actor_kind, entry.reason) == ("person.created", person.id, "system", "first sign-in")


def test_a_later_sign_in_keeps_the_person_and_audits_only_a_role_change(store):
    first, _, _ = sign_in(store)
    again, _, _ = sign_in(store, now=T0 + HOUR, name="Pat Example")
    assert again.id == first.id and again.display_name == "Pat Example" and len(store.audit_entries()) == 1
    sign_in(store, roles=["admin"], groups=["golden-road-admins"], now=T0 + 2 * HOUR)
    latest = store.audit_entries()[0]
    assert latest.action == "person.roles_changed" and latest.before == {"roles": []} and latest.after == {"roles": ["admin"]}


def test_sessions_expire_on_idle_and_on_age_and_end_on_sign_out(store):
    person, token, csrf = sign_in(store, roles=["ops"], groups=["g-ops"])
    session = store.get_session(token, now=T0 + dt.timedelta(minutes=50), idle=HOUR)
    assert (session.person_id, session.roles, session.groups, session.csrf) == (person.id, ("ops",), ("g-ops",), csrf)
    assert store.get_session(token, now=T0 + dt.timedelta(minutes=100), idle=HOUR) is not None  # 50 minutes since it was last seen
    assert store.get_session(token, now=T0 + dt.timedelta(minutes=161), idle=HOUR) is None      # idle more than an hour
    _, token2, _ = sign_in(store, now=T0)
    for minutes in range(50, 8 * 60, 50):
        assert store.get_session(token2, now=T0 + dt.timedelta(minutes=minutes), idle=HOUR) is not None
    assert store.get_session(token2, now=T0 + DAY, idle=HOUR) is None                            # 8 hours, however active
    _, token3, _ = sign_in(store, now=T0)
    store.end_session(token3)
    assert store.get_session(token3, now=T0, idle=HOUR) is None
    assert store.get_session("not-a-token", now=T0, idle=HOUR) is None


def test_a_login_state_is_single_use_bound_to_its_kind_and_expires(store):
    state = LoginState("oidc", "nonce-1", "verifier-1", "/register", None)
    store.put_login_state("s1", state, expires_at=T0 + dt.timedelta(minutes=10))
    assert store.take_login_state("s1", kind="github", now=T0) is None
    store.put_login_state("s2", state, expires_at=T0 + dt.timedelta(minutes=10))
    assert store.take_login_state("s2", kind="oidc", now=T0) == state
    assert store.take_login_state("s2", kind="oidc", now=T0) is None
    store.put_login_state("s3", state, expires_at=T0 + dt.timedelta(minutes=10))
    assert store.take_login_state("s3", kind="oidc", now=T0 + dt.timedelta(minutes=11)) is None


def test_people_are_listed_by_name(store):
    sign_in(store, "sub-b", name="Bea")
    sign_in(store, "sub-a", name="Al")
    assert [p.display_name for p in store.list_people()] == ["Al", "Bea"]


from portal.store.base import RepoAdmin  # noqa: E402


def test_a_login_is_linked_to_at_most_one_person(store):
    pat, _, _ = sign_in(store, "sub-pat")
    sam, _, _ = sign_in(store, "sub-sam")
    link = store.add_link(person_id=pat.id, github_login="Pat-Example", github_user_id=1001, source="github-oauth",
                          actor_id=pat.id, reason="the person authorized the GitHub App as this login", now=T0)
    assert link.id.startswith("l_") and store.links_for(pat.id) == [link]
    for login, uid in (("pat-example", None), ("someone-else", 1001)):
        with pytest.raises(Conflict) as err:
            store.add_link(person_id=sam.id, github_login=login, github_user_id=uid, source="admin", actor_id=pat.id, reason="r" * 10, now=T0)
        assert err.value.code == "link_taken"
    entry = store.audit_entries()[0]
    assert (entry.action, entry.actor_id, entry.after["github_login"], entry.after["source"]) == ("identity_link.created", pat.id, "Pat-Example", "github-oauth")


def test_removing_a_link_frees_the_login_and_is_audited(store):
    pat, _, _ = sign_in(store, "sub-pat")
    admin, _, _ = sign_in(store, "sub-admin", roles=["admin"])
    link = store.add_link(person_id=pat.id, github_login="pat-example", github_user_id=None, source="admin", actor_id=admin.id,
                          reason="confirmed in person", now=T0)
    store.remove_link(link_id=link.id, actor_id=admin.id, reason="linked to the wrong person", now=T0)
    assert store.links_for(pat.id) == [] and store.active_links() == []
    entry = store.audit_entries()[0]
    assert (entry.action, entry.before["github_login"], entry.reason) == ("identity_link.removed", "pat-example", "linked to the wrong person")
    with pytest.raises(Conflict):
        store.remove_link(link_id=link.id, actor_id=admin.id, reason="again", now=T0)
    store.add_link(person_id=pat.id, github_login="pat-example", github_user_id=7, source="github-oauth", actor_id=pat.id, reason="relinked", now=T0)


def test_a_link_needs_a_known_person_and_source(store):
    with pytest.raises(Conflict):
        store.add_link(person_id="p_" + "0" * 32, github_login="x", github_user_id=None, source="admin", actor_id=None, reason="r" * 10, now=T0)
    pat, _, _ = sign_in(store)
    with pytest.raises(ValueError):
        store.add_link(person_id=pat.id, github_login="x", github_user_id=None, source="claim", actor_id=pat.id, reason="r" * 10, now=T0)


def test_repo_admins_are_replaced_as_a_whole(store):
    store.replace_repo_admins([RepoAdmin("acme/notes", "pat", 1001, T0), RepoAdmin("acme/Zeta", "pat", 1001, T0),
                               RepoAdmin("acme/notes", "sam", 1002, T0)], observed_at=T0)
    assert [r.repo for r in store.admin_repos([1001])] == ["acme/notes", "acme/Zeta"]
    later = T0 + HOUR
    store.replace_repo_admins([RepoAdmin("acme/notes", "sam", 1002, later)], observed_at=later)
    assert store.admin_repos([1001]) == [] and store.admin_repos([1002])[0].observed_at == later



def test_a_person_s_admin_repos_come_from_their_active_links_by_user_id_or_by_login(store):
    """What the writer reads to re-check a binding's requester (it can't read the links or the repo admins themselves)."""
    pat, _, _ = sign_in(store)
    sam, _, _ = sign_in(store, subject="sub-2", name="Sam")
    lead, _, _ = sign_in(store, subject="sub-3", name="Lead", roles=["admin"], groups=["golden-road-admins"])
    by_id = store.add_link(person_id=pat.id, github_login="pat-gh", github_user_id=1001, source="admin", actor_id=lead.id, reason="r" * 10, now=T0)
    store.add_link(person_id=pat.id, github_login="Pat-Old", github_user_id=None, source="admin", actor_id=lead.id, reason="r" * 10, now=T0)
    store.add_link(person_id=sam.id, github_login="sam-gh", github_user_id=1002, source="admin", actor_id=lead.id, reason="r" * 10, now=T0)
    store.replace_repo_admins([RepoAdmin("acme/notes", "renamed-pat", 1001, T0), RepoAdmin("acme/Old", "pat-old", 1003, T0),
                               RepoAdmin("acme/sams", "sam-gh", 1002, T0), RepoAdmin("acme/byname", "pat-gh", 1009, T0)], observed_at=T0)
    assert [r.repo for r in store.admin_repos_of_person(pat.id)] == ["acme/notes", "acme/Old"], "by user ID, or by login without one"
    assert store.admin_repos_of_person(pat.id)[0].observed_at == T0
    store.remove_link(link_id=by_id.id, actor_id=lead.id, reason="r" * 10, now=T0)
    assert [r.repo for r in store.admin_repos_of_person(pat.id)] == ["acme/Old"], "a removed link no longer counts"
    assert store.admin_repos_of_person("p_" + "0" * 32) == []


def test_the_web_s_admin_repos_and_the_writer_s_agree(store):
    """The web decides with ownership.admin_repos_of, the writer re-checks with store.admin_repos_of_person: the same repos, seen at the
    same time, for every person (one row per repo on the web, one per matching login in the store)."""
    from portal.web.ownership import admin_repos_of
    pat, _, _ = sign_in(store)
    sam, _, _ = sign_in(store, subject="sub-2", name="Sam")
    kim, _, _ = sign_in(store, subject="sub-4", name="Kim")
    lead, _, _ = sign_in(store, subject="sub-3", name="Lead", roles=["admin"], groups=["golden-road-admins"])
    store.add_link(person_id=pat.id, github_login="pat-gh", github_user_id=1001, source="admin", actor_id=lead.id, reason="r" * 10, now=T0)
    store.add_link(person_id=pat.id, github_login="Pat-Old", github_user_id=None, source="admin", actor_id=lead.id, reason="r" * 10, now=T0)
    gone = store.add_link(person_id=sam.id, github_login="sam-gh", github_user_id=1002, source="admin", actor_id=lead.id, reason="r" * 10, now=T0)
    store.replace_repo_admins([RepoAdmin("acme/notes", "renamed-pat", 1001, T0), RepoAdmin("acme/notes", "pat-old", 1003, T0),
                               RepoAdmin("acme/Old", "PAT-OLD", 1003, T0), RepoAdmin("acme/zeta", "pat-gh", 1001, T0),
                               RepoAdmin("acme/sams", "sam-gh", 1002, T0), RepoAdmin("acme/byname", "pat-gh", 1009, T0)], observed_at=T0)

    def agree():
        for person in (pat, sam, kim, lead):
            web = {(r.repo, r.observed_at) for r in admin_repos_of(store, person.id)}
            writer = {(r.repo, r.observed_at) for r in store.admin_repos_of_person(person.id)}
            assert web == writer, person.display_name
    agree()
    assert {r.repo for r in admin_repos_of(store, pat.id)} == {"acme/notes", "acme/Old", "acme/zeta"}
    store.remove_link(link_id=gone.id, actor_id=lead.id, reason="r" * 10, now=T0)
    agree()

DRAFT = {"schema": "registration-draft/v1", "repo": "acme/notes", "purpose": "Stores short team notes.",
         "data_classification": "public"}


def final_for(evidence, **changes):
    """The final record an approval carries: the store checks its tier, classification and the evidence it names (the rest is the web's)."""
    return {"schema": "intake/v2", "tier": "moderate", "data_classification": "public", "approved_at": "2026-10-23T12:00:00Z", "evidence_commit": evidence.commit,
            "floor_tier": evidence.floor_tier, "floor_classification": evidence.floor_classification, **changes}


def people(store):
    owner, _, _ = sign_in(store, "sub-owner")
    admin, _, _ = sign_in(store, "sub-admin", roles=["admin"], groups=["golden-road-admins"])
    return owner, admin


def test_a_registration_goes_from_pending_to_merged_and_every_step_is_audited(store):
    owner, admin = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    assert (req.state, req.repo, req.requested_by, req.draft) == ("pending", "acme/notes", owner.id, DRAFT)
    final = final_for(give_evidence(store, req))
    approved = store.decide(request_id=req.id, actor_id=admin.id, actor_groups=["golden-road-admins"], approve=True,
                            note="Purpose checked.", final=final, now=T0 + HOUR)
    assert (approved.state, approved.decided_by, approved.decision_groups, approved.final) == ("approved", admin.id, ("golden-road-admins",), final)
    claimed = store.claim_approved(now=T0 + 2 * HOUR)
    assert claimed.id == req.id and claimed.state == "writing" and store.claim_approved(now=T0 + 2 * HOUR) is None
    opened = store.mark_pr_open(request_id=req.id, branch="portal/request-x", pr_number=12, pr_url="https://github.com/acme/scorecard-factory/pull/12",
                               head_sha="2" * 40, base_sha="1" * 40, now=T0 + 3 * HOUR)
    assert (opened.state, opened.pr_number, opened.head_sha, opened.base_sha) == ("pr_open", 12, "2" * 40, "1" * 40)
    assert store.get_request(req.id).head_sha == "2" * 40
    merged = store.mark_merged(request_id=req.id, merge_sha="d" * 40, now=T0 + 4 * HOUR)
    assert (merged.state, merged.merge_sha) == ("merged", "d" * 40)
    actions = [e.action for e in reversed(store.audit_entries()) if e.subject_kind == "request"]
    assert actions == ["request.created", "request.approved", "request.writing", "request.pr_open", "request.merged"]
    assert all(e.reason for e in store.audit_entries())


def test_only_one_open_registration_per_repo(store):
    owner, admin = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    with pytest.raises(Conflict) as err:
        store.create_registration(person_id=admin.id, draft={**DRAFT, "repo": "ACME/Notes"}, now=T0)
    assert err.value.code == "open_request_exists"
    store.decide(request_id=req.id, actor_id=admin.id, actor_groups=[], approve=True, note="ok", final=final_for(give_evidence(store, req)), now=T0)
    store.claim_approved(now=T0)
    store.mark_failed(request_id=req.id, reason="check_failed: factory-check", now=T0)
    store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)  # a failed one no longer blocks


def test_only_a_pending_request_is_decided_and_never_by_its_requester(store):
    owner, admin = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    final = final_for(give_evidence(store, req))
    for actor in (owner,):
        with pytest.raises(Conflict) as err:
            store.decide(request_id=req.id, actor_id=actor.id, actor_groups=[], approve=True, note="mine", final=final, now=T0)
        assert err.value.code == "self_decision"
    store.decide(request_id=req.id, actor_id=admin.id, actor_groups=[], approve=False, note="Name what it does not do.", final=None, now=T0)
    with pytest.raises(Conflict) as err:
        store.decide(request_id=req.id, actor_id=admin.id, actor_groups=[], approve=True, note="x", final=final, now=T0)
    assert err.value.code == "wrong_state"
    with pytest.raises(Conflict):
        store.decide(request_id="r_" + "0" * 32, actor_id=admin.id, actor_groups=[], approve=True, note="x", final=final, now=T0)


def test_a_sent_back_request_is_resubmitted_by_its_requester_only(store):
    owner, admin = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    with pytest.raises(Conflict):
        store.resubmit(request_id=req.id, person_id=owner.id, draft=DRAFT, now=T0)  # still pending
    store.decide(request_id=req.id, actor_id=admin.id, actor_groups=[], approve=False, note="Say what it does not do.", final=None, now=T0)
    with pytest.raises(Conflict) as err:
        store.resubmit(request_id=req.id, person_id=admin.id, draft=DRAFT, now=T0)
    assert err.value.code == "not_found"
    with pytest.raises(Conflict) as err:
        store.resubmit(request_id=req.id, person_id=owner.id, draft={**DRAFT, "repo": "acme/other"}, now=T0)
    assert err.value.code == "repo_changed"
    again = store.resubmit(request_id=req.id, person_id=owner.id, draft={**DRAFT, "purpose": "Stores short notes; no email."}, now=T0 + HOUR)
    assert (again.state, again.decided_by, again.draft["purpose"]) == ("pending", None, "Stores short notes; no email.")


def test_transitions_out_of_order_are_refused(store):
    owner, admin = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    for call in (lambda: store.mark_pr_open(request_id=req.id, branch="b", pr_number=1, pr_url="u", head_sha="2" * 40, base_sha="1" * 40, now=T0),
                 lambda: store.mark_merged(request_id=req.id, merge_sha="d" * 40, now=T0),
                 lambda: store.mark_failed(request_id=req.id, reason="x", now=T0)):
        with pytest.raises(Conflict):
            call()


def test_requests_are_listed_by_requester_and_by_state(store):
    owner, admin = people(store)
    a = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    b = store.create_registration(person_id=owner.id, draft={**DRAFT, "repo": "acme/two"}, now=T0 + HOUR)
    assert [r.id for r in store.requests_by(owner.id)] == [b.id, a.id]
    assert [r.id for r in store.requests_in(("pending",))] == [a.id, b.id]
    assert store.requests_by(admin.id) == [] and store.get_request(a.id) == a


def test_admin_repos_by_login_matches_case_insensitively(store):
    store.replace_repo_admins([RepoAdmin("acme/notes", "Pat", 1001, T0), RepoAdmin("acme/Zeta", "pat", 1001, T0), RepoAdmin("acme/other", "sam", 1002, T0)],
                              observed_at=T0)
    assert [r.repo for r in store.admin_repos_by_login(["pat"])] == ["acme/notes", "acme/Zeta"]
    assert store.admin_repos_by_login([]) == [] and store.admin_repos_by_login(["nobody"]) == []


def test_observed_repo_is_the_collector_s_name_of_a_repo(store):
    store.replace_repo_admins([RepoAdmin("acme/Zeta", "pat", 1001, T0), RepoAdmin("acme/Zeta", "sam", 1002, T0)], observed_at=T0)
    assert store.observed_repo("ACME/zeta") == "acme/Zeta" and store.observed_repo("acme/Zeta") == "acme/Zeta"
    assert store.observed_repo("acme/zet") is None and store.observed_repo("acme/zet_") is None and store.observed_repo("%") is None


def test_an_admin_cannot_link_a_login_to_their_own_person(store):
    admin, _, _ = sign_in(store, "sub-admin", roles=["admin"])
    with pytest.raises(Conflict) as err:
        store.add_link(person_id=admin.id, github_login="me-gh", github_user_id=None, source="admin", actor_id=admin.id, reason="r" * 10, now=T0)
    assert err.value.code == "self_link" and store.links_for(admin.id) == []
    store.add_link(person_id=admin.id, github_login="me-gh", github_user_id=5, source="github-oauth", actor_id=admin.id, reason="authorized", now=T0)


# ---- registration evidence (spec 2026-10-23): one row per submission, insert-only, plus the reasons it is waiting ----

import dataclasses  # noqa: E402

from portal_fakes import give_evidence  # noqa: E402


def test_a_pending_registration_awaits_evidence_until_it_has_some(store):
    owner, admin = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    assert [(a.request_id, a.repo, a.submission) for a in store.awaiting_evidence()] == [(req.id, "acme/notes", 1)]
    store.add_evidence_problem(request_id=req.id, submission=1, problem="rules_missing", now=T0)
    store.add_evidence_problem(request_id=req.id, submission=1, problem="rules_missing", now=T0)  # once only, no error
    assert store.evidence_problem_for(req.id, 1) == "rules_missing" and store.awaiting_evidence()
    give_evidence(store, req, floor_tier="moderate")
    assert store.awaiting_evidence() == [] and store.evidence_for(req.id, 1).floor_tier == "moderate"
    with pytest.raises(Conflict, match="evidence_exists"):
        give_evidence(store, req)


def test_a_resubmission_counts_up_and_awaits_new_evidence(store):
    owner, admin = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    give_evidence(store, req)
    store.decide(request_id=req.id, actor_id=admin.id, actor_groups=["g"], approve=False, note="Say what it stores.", final=None, now=T0)
    again = store.resubmit(request_id=req.id, person_id=owner.id, draft=DRAFT, now=T0)
    assert again.submission == 2 and store.evidence_for(req.id, 2) is None and store.evidence_for(req.id, 1) is not None
    assert [a.submission for a in store.awaiting_evidence()] == [2]


def test_evidence_is_only_for_a_pending_request_s_current_submission(store):
    owner, admin = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    with pytest.raises(Conflict, match="not_awaiting"):
        give_evidence(store, dataclasses.replace(req, submission=2))
    store.decide(request_id=req.id, actor_id=admin.id, actor_groups=["g"], approve=False, note="Say what it stores.", final=None, now=T0)
    with pytest.raises(Conflict, match="not_awaiting"):
        give_evidence(store, req)


# ---- the store enforces the floor on approval (spec E5): evidence of the current submission, the stored commit and floor, an override below ----

from portal.store.base import Evidence  # noqa: E402

LONG = "The spec over-declares egress; checked with the team."


def override(by, reason=LONG):
    return {"by": by, "at": "2026-10-23T12:00:00Z", "reason": reason}


def approve(store, req, actor, final, floor_override=None):
    return store.decide(request_id=req.id, actor_id=actor.id, actor_groups=["g"], approve=True, note="Checked.", final=final,
                        floor_override=floor_override, now=T0)


def test_an_approval_needs_the_evidence_of_the_current_submission(store):
    owner, admin = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    stub = Evidence(req.id, 1, "e" * 40, {}, "low", "public", "f" * 40, T0)
    with pytest.raises(Conflict, match="no_evidence"):
        approve(store, req, admin, final_for(stub))
    give_evidence(store, req)
    store.decide(request_id=req.id, actor_id=admin.id, actor_groups=["g"], approve=False, note="Say what it stores.", final=None, now=T0)
    store.resubmit(request_id=req.id, person_id=owner.id, draft=DRAFT, now=T0)
    with pytest.raises(Conflict, match="no_evidence"):  # submission 1's evidence doesn't count for submission 2
        approve(store, req, admin, final_for(stub))
    assert store.get_request(req.id).state == "pending"


@pytest.mark.parametrize("change", [{"evidence_commit": "d" * 40}, {"floor_tier": "high"}, {"floor_classification": "protected"}])
def test_the_approved_record_must_carry_the_stored_commit_and_floor(store, change):
    owner, admin = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    final = final_for(give_evidence(store, req, floor_tier="moderate"), **change)
    with pytest.raises(Conflict, match="evidence_mismatch"):
        approve(store, req, admin, final)
    assert store.get_request(req.id).state == "pending"


def test_an_approval_below_the_floor_needs_the_approver_s_override(store):
    owner, admin = people(store)
    other, _, _ = sign_in(store, "sub-other", roles=["admin"], groups=["g"])
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    final = final_for(give_evidence(store, req, floor_tier="high"))
    for given in (None, override(admin.id, "x" * 29), override(other.id)):
        with pytest.raises(Conflict, match="below_floor"):
            approve(store, req, admin, {**final, **({"floor_override": given} if given else {})}, given)
    assert store.get_request(req.id).state == "pending"
    good = override(admin.id)
    approved = approve(store, req, admin, {**final, "floor_override": good}, good)
    assert approved.state == "approved" and approved.floor_override == good


@pytest.mark.parametrize("change", [{"at": "2026-10-24T12:00:00Z"}, {"reason": LONG + "\nand a second line"}, {"reason": "r" * 1001}])
def test_the_override_must_carry_the_approval_time_and_a_single_line_of_at_most_1000_characters(store, change):
    owner, admin = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    final = final_for(give_evidence(store, req, floor_tier="high"))
    bad = {**override(admin.id), **change}
    with pytest.raises(Conflict, match="below_floor"):
        approve(store, req, admin, {**final, "floor_override": bad}, bad)
    assert store.get_request(req.id).state == "pending"


def test_an_override_is_refused_when_nothing_is_below_the_floor(store):
    owner, admin = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    final = final_for(give_evidence(store, req, floor_tier="low"))
    unneeded = override(admin.id)
    with pytest.raises(Conflict, match="below_floor"):
        approve(store, req, admin, {**final, "floor_override": unneeded}, unneeded)


def test_the_override_and_the_record_must_agree(store):
    owner, admin = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    final = final_for(give_evidence(store, req, floor_tier="high"))
    with pytest.raises(Conflict, match="evidence_mismatch"):
        approve(store, req, admin, {**final, "floor_override": override(admin.id)}, override(admin.id, "A different reason, also long enough."))


def test_the_override_is_stored_and_audited(store):
    owner, admin = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    final = final_for(give_evidence(store, req, floor_tier="high"))
    good = override(admin.id)
    approve(store, req, admin, {**final, "floor_override": good}, good)
    assert store.get_request(req.id).floor_override == good
    entry = next(e for e in store.audit_entries() if e.action == "request.approved")
    assert entry.after["floor_override"] == good and entry.after["final"]["floor_override"] == good


def test_requests_with_a_waiting_reason_do_not_starve_new_ones(store):
    owner, _ = people(store)
    made = [store.create_registration(person_id=owner.id, draft={**DRAFT, "repo": f"acme/r{i:02d}"}, now=T0) for i in range(51)]
    ordered = sorted(made, key=lambda r: r.id)
    for req in ordered[:50]:  # 50 requests whose repos the collector can't read, ahead of the 51st by id
        store.add_evidence_problem(request_id=req.id, submission=1, problem="repo_unreadable", now=T0)
    first = store.awaiting_evidence(limit=50)
    assert len(first) == 50 and first[0].request_id == ordered[50].id  # the one without a problem row comes first
    assert [a.request_id for a in first[1:]] == [r.id for r in ordered[:49]]  # then the rest by id


# ---- binding requests (spec 2026-10-29 app bindings, B1): the app's admin's change starts approved, with no decision ----

from portal.ids import new_id  # noqa: E402


def binding_final(repo, person_id, request_id, environment="test"):
    return {"schema": "binding/v2", "repo": repo, "environment": environment, "secrets": [], "linked_by": person_id, "linked_at": "2026-10-23T12:00:00Z", "request": request_id,
            "azure_identity": {"directory_id": "sp-notes", "tenant_id": "00000000-0000-4000-8000-00000000a2e0",
                               "client_id": "11111111-1111-4111-8111-111111111111", "verified_by": "mock-directory"}}


def test_a_binding_request_starts_approved_without_a_decision_and_is_audited(store):
    person, _, _ = sign_in(store)
    rid = new_id("r")
    final = binding_final("acme/Notes", person.id, rid)
    req = store.create_binding(request_id=rid, person_id=person.id, final=final, now=T0)
    assert (req.id, req.kind, req.repo, req.state, req.requested_by) == (rid, "binding", "acme/Notes", "approved", person.id)
    assert (req.decided_by, req.decided_at, req.decision_note, req.floor_override) == (None, None, None, None)
    assert req.final == req.draft == final and store.get_request(rid) == req
    entry = store.audit_entries()[0]
    assert (entry.action, entry.subject_id, entry.actor_id, entry.reason) == ("request.created", rid, person.id, "connection change requested in the portal")
    assert (entry.before, entry.after) == ({"binding": None}, {"state": "approved", "repo": "acme/Notes", "final": final})
    assert store.awaiting_evidence() == []   # the collector reads registrations only
    with pytest.raises(Conflict, match="wrong_state"):   # nobody decides a binding
        store.decide(request_id=rid, actor_id=person.id, actor_groups=[], approve=True, note="x", final=final, now=T0)


def test_one_open_binding_request_per_repo(store):
    person, _, _ = sign_in(store)
    first = new_id("r")
    store.create_binding(request_id=first, person_id=person.id, final=binding_final("acme/notes", person.id, first), now=T0)
    second = new_id("r")
    with pytest.raises(Conflict) as exc:
        store.create_binding(request_id=second, person_id=person.id, final=binding_final("ACME/notes", person.id, second), now=T0)
    assert exc.value.code == "open_request_exists" and store.get_request(second) is None
    store.create_registration(person_id=person.id, draft={**DRAFT, "repo": "acme/notes"}, now=T0)  # a registration is another kind
    store.claim_approved(now=T0)
    store.mark_failed(request_id=first, reason="the writer could not write it", now=T0)
    third = new_id("r")
    store.create_binding(request_id=third, person_id=person.id, final=binding_final("acme/notes", person.id, third), now=T0)  # a failed one no longer blocks


def test_the_writer_claims_bindings_in_creation_order_alongside_registrations(store):
    owner, admin = people(store)
    a, b = new_id("r"), new_id("r")
    store.create_binding(request_id=a, person_id=owner.id, final=binding_final("acme/a", owner.id, a), now=T0)
    reg = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    evidence = give_evidence(store, reg)
    store.decide(request_id=reg.id, actor_id=admin.id, actor_groups=["g"], approve=True, note="ok", final=final_for(evidence), now=T0 + HOUR / 2)
    store.create_binding(request_id=b, person_id=owner.id, final=binding_final("acme/b", owner.id, b), now=T0 + HOUR)
    claimed = [store.claim_approved(now=T0 + 2 * HOUR).id for _ in range(3)]
    assert claimed == [a, reg.id, b] and store.claim_approved(now=T0 + 2 * HOUR) is None


def test_requests_for_repo_lists_both_kinds_newest_first(store):
    person, _, _ = sign_in(store)
    reg = store.create_registration(person_id=person.id, now=T0, draft={"schema": "registration-draft/v1", "repo": "acme/notes",
                                    "purpose": "Stores short team notes.", "data_classification": "public"})
    rid = new_id("r")
    store.create_binding(request_id=rid, person_id=person.id, final=binding_final("acme/notes", person.id, rid), now=T0 + HOUR)
    other = new_id("r")
    store.create_binding(request_id=other, person_id=person.id, final=binding_final("acme/other", person.id, other), now=T0 + HOUR)
    assert [r.id for r in store.requests_for_repo("ACME/Notes")] == [rid, reg.id]
    assert store.requests_for_repo("acme/nothing") == []


@pytest.mark.parametrize("change", [
    {"request": "r_" + "9" * 32},          # names another request
    {"linked_by": "p_" + "9" * 32},        # names another person
    {"schema": "intake/v2"},
    {"repo": None},
])
def test_a_binding_final_must_name_its_request_its_person_and_its_repo(store, change):
    person, _, _ = sign_in(store)
    rid = new_id("r")
    with pytest.raises(ValueError):
        store.create_binding(request_id=rid, person_id=person.id, final={**binding_final("acme/notes", person.id, rid), **change}, now=T0)
    assert store.get_request(rid) is None and [e.action for e in store.audit_entries()] == ["person.created"]


def test_the_audit_entry_records_the_binding_it_replaces(store):
    person, _, _ = sign_in(store)
    first = new_id("r")
    current = binding_final("acme/notes", person.id, first)
    store.create_binding(request_id=first, person_id=person.id, final=current, now=T0)
    store.claim_approved(now=T0)
    store.mark_failed(request_id=first, reason="the writer could not write it", now=T0)
    second = new_id("r")
    store.create_binding(request_id=second, person_id=person.id, final=binding_final("acme/notes", person.id, second), current=current, now=T0)
    entry = store.audit_entries()[0]
    assert (entry.subject_id, entry.before) == (second, {"binding": current})


def test_a_binding_never_reuses_a_request_id(store):
    person, _, _ = sign_in(store)
    reg = store.create_registration(person_id=person.id, draft={**DRAFT, "repo": "acme/registered"}, now=T0)
    with pytest.raises(Conflict) as exc:   # a registration's id, for another repo: not an open-request clash
        store.create_binding(request_id=reg.id, person_id=person.id, final=binding_final("acme/notes", person.id, reg.id), now=T0)
    assert exc.value.code == "request_exists" and store.get_request(reg.id).kind == "registration"
    rid = new_id("r")
    store.create_binding(request_id=rid, person_id=person.id, final=binding_final("acme/a", person.id, rid), now=T0)
    with pytest.raises(Conflict) as exc:
        store.create_binding(request_id=rid, person_id=person.id, final=binding_final("acme/b", person.id, rid), now=T0)
    assert exc.value.code == "request_exists" and store.get_request(rid).repo == "acme/a"


@pytest.mark.parametrize("request_id", ["r_" + "A" * 32, "r_123", "x_" + "a" * 32, "r_" + "a" * 32 + "\n"])
def test_a_binding_request_id_has_the_portal_s_format(store, request_id):
    person, _, _ = sign_in(store)
    with pytest.raises(ValueError):
        store.create_binding(request_id=request_id, person_id=person.id, final=binding_final("acme/notes", person.id, request_id), now=T0)


@pytest.mark.parametrize("change", [{"token": "x"}, {"linked_at": "yesterday"}, {"repo": "notes"},
                                    {"databricks": {"directory_id": "wh-notes", "host": "evil.example.com", "http_path": "/x", "verified_by": "mock-directory"}}])
def test_a_binding_record_must_be_a_valid_binding_v2(store, change):
    person, _, _ = sign_in(store)
    rid = new_id("r")
    with pytest.raises(ValueError):
        store.create_binding(request_id=rid, person_id=person.id, final={**binding_final("acme/notes", person.id, rid), **change}, now=T0)
    assert store.get_request(rid) is None


def test_a_binding_by_an_unknown_person_is_refused_cleanly(store):
    nobody, rid = "p_" + "0" * 32, new_id("r")
    with pytest.raises(Conflict) as exc:
        store.create_binding(request_id=rid, person_id=nobody, final=binding_final("acme/notes", nobody, rid), now=T0)
    assert exc.value.code == "unknown_person" and store.get_request(rid) is None and store.audit_entries() == []


# ---- per environment (spec 2026-10-29 GitOps deploy repo, G3; migration 0007) ----

def test_a_test_and_a_prod_binding_for_one_repo_may_both_be_open(store):
    person, _, _ = sign_in(store)
    test, prod = new_id("r"), new_id("r")
    store.create_binding(request_id=test, person_id=person.id, final=binding_final("acme/notes", person.id, test, "test"), now=T0)
    store.create_binding(request_id=prod, person_id=person.id, final=binding_final("ACME/Notes", person.id, prod, "prod"), now=T0)
    assert {store.get_request(test).state, store.get_request(prod).state} == {"approved"}


@pytest.mark.parametrize("environment", ["test", "prod"])
def test_two_open_bindings_for_one_repo_and_environment_may_not(store, environment):
    person, _, _ = sign_in(store)
    first, second = new_id("r"), new_id("r")
    store.create_binding(request_id=first, person_id=person.id, final=binding_final("acme/notes", person.id, first, environment), now=T0)
    with pytest.raises(Conflict) as exc:
        store.create_binding(request_id=second, person_id=person.id, final=binding_final("Acme/notes", person.id, second, environment), now=T0)
    assert exc.value.code == "open_request_exists" and store.get_request(second) is None


def test_a_binding_v1_record_is_refused(store):
    person, _, _ = sign_in(store)
    rid = new_id("r")
    v1 = {k: v for k, v in binding_final("acme/notes", person.id, rid).items() if k not in ("environment", "secrets")}
    with pytest.raises(ValueError):
        store.create_binding(request_id=rid, person_id=person.id, final={**v1, "schema": "binding/v1"}, now=T0)
    assert store.get_request(rid) is None and [e.action for e in store.audit_entries()] == ["person.created"]


@pytest.mark.parametrize("environment", ["staging", "", "Test", None])
def test_a_binding_for_an_environment_other_than_test_or_prod_is_refused(store, environment):
    person, _, _ = sign_in(store)
    rid = new_id("r")
    final = binding_final("acme/notes", person.id, rid, environment)
    if environment is None:
        del final["environment"]
    with pytest.raises(ValueError):
        store.create_binding(request_id=rid, person_id=person.id, final=final, now=T0)
    assert store.get_request(rid) is None


def test_an_environment_key_in_a_registration_draft_does_not_open_a_second_registration(store):
    person, _, _ = sign_in(store)
    store.create_registration(person_id=person.id, draft={**DRAFT, "repo": "acme/notes"}, now=T0)
    with pytest.raises(Conflict) as exc:
        store.create_registration(person_id=person.id, draft={**DRAFT, "repo": "acme/notes", "environment": "prod"}, now=T0)
    assert exc.value.code == "open_request_exists"


def test_audit_for_lists_one_subject_s_entries_oldest_first(store):
    owner, _, _ = sign_in(store, subject="sub-owner")
    admin, _, _ = sign_in(store, subject="sub-admin", roles=["admin"], groups=["g"])
    draft = {"schema": "registration-draft/v1", "repo": "acme/notes", "purpose": "Stores short team notes.", "data_classification": "public"}
    req = store.create_registration(person_id=owner.id, draft=draft, now=T0)
    other = store.create_registration(person_id=owner.id, draft={**draft, "repo": "acme/tools"}, now=T0)
    store.decide(request_id=req.id, actor_id=admin.id, actor_groups=["g"], approve=False, note="Say what it does not do.", final=None,
                 now=T0 + HOUR)
    entries = store.audit_for("request", req.id)
    assert [e.action for e in entries] == ["request.created", "request.sent_back"] and entries[0].at <= entries[1].at
    assert [e.action for e in store.audit_for("request", other.id)] == ["request.created"]
    assert store.audit_for("request", "r_" + "0" * 32) == []


# ---- registration changes and the re-review list (spec 2026-10-31) ----

from portal.store.base import IntakeObserved  # noqa: E402

RECORD = {"schema": "intake/v2", "by_portal": True, "tier": "moderate", "data_classification": "public",
          "purpose": "Stores short team notes.", "owner": "p_" + "1" * 32, "review_by": "2027-01-01"}


def observed(repo="acme/notes", owner="p_" + "1" * 32, review_by=dt.date(2026, 11, 1), **changes):
    base = dict(repo=repo, blob_sha="c" * 40, schema="intake/v2", by_portal=True, owner=owner, tier="moderate", data_classification="public",
                purpose="Stores short team notes.", review_by=review_by, scorecard=None, problem=None)
    return IntakeObserved(**{**base, **changes})


def event_doc(repo="acme/notes", head="a" * 40, pr=7, signals=None):
    return {"schema": "re-review-event/v1", "repo": repo, "pr": pr, "head_sha": head, "tree_sha": "b" * 40, "scorecard_version": "0.5.1",
            "created": "2026-10-31T12:00:00Z", "level": "elevated", "floor": "standard", "complete": True,
            "signals": signals if signals is not None else [{"id": "T-17", "kind": "trigger", "level": "elevated", "summary": "raised", "count": 1}]}


def test_evidence_of_a_change_carries_the_record_it_replaces(store):
    owner, _ = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    found = give_evidence(store, req, record_sha="c" * 40, record=RECORD)
    assert (found.record_sha, found.record) == ("c" * 40, RECORD)
    assert (store.evidence_for(req.id, 1).record_sha, store.evidence_for(req.id, 1).record) == ("c" * 40, RECORD)


def test_a_first_registration_s_evidence_has_no_record_and_a_half_record_is_refused(store):
    owner, _ = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    for sha, record in (("c" * 40, None), (None, RECORD), ("not-a-sha", RECORD), ("c" * 40, {"schema": "intake/v9"})):
        with pytest.raises(ValueError):
            give_evidence(store, req, record_sha=sha, record=record)
    assert give_evidence(store, req).record_sha is None and store.evidence_for(req.id, 1).record is None


def test_a_record_that_can_t_be_read_is_a_reason_to_wait(store):
    owner, _ = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    store.add_evidence_problem(request_id=req.id, submission=1, problem="record_unreadable", now=T0)
    assert store.evidence_problem_for(req.id, 1) == "record_unreadable"


def test_the_observed_intake_records_are_replaced_whole_and_found_in_any_case(store):
    store.replace_intake_observed([observed(), observed("acme/Tools", review_by=None, schema="intake/v1", by_portal=False, owner="team")],
                                  observed_at=T0)
    assert [r.repo for r in store.intake_observed()] == ["acme/notes", "acme/Tools"]
    assert [r.repo for r in store.intake_observed("ACME/tools")] == ["acme/Tools"]
    store.replace_intake_observed([observed("acme/zeta", problem="invalid", schema=None, owner=None, tier=None, data_classification=None,
                                            purpose=None, review_by=None, by_portal=False)], observed_at=T0 + HOUR)
    [only] = store.intake_observed()
    assert (only.repo, only.problem, only.schema) == ("acme/zeta", "invalid", None)


def test_observed_rows_must_be_well_formed(store):
    for bad in (observed(repo="acme"), observed(blob_sha="x"), observed(problem="eh"), observed(schema="intake/v2", review_by=None),
                [observed(), observed("ACME/notes")]):
        with pytest.raises(ValueError):
            store.replace_intake_observed(bad if isinstance(bad, list) else [bad], observed_at=T0)


def test_each_run_is_recorded_once_and_counted(store):
    store.record_rereview_run(repo="acme/scorecard-factory", run_id=11, outcome="event", now=T0)
    store.record_rereview_run(repo="acme/scorecard-factory", run_id=12, outcome="invalid", now=T0)
    store.record_rereview_run(repo="acme/notes", run_id=11, outcome="invalid", now=T0)
    assert store.rereview_runs_seen("acme/scorecard-factory", [11, 12, 13]) == {11, 12}
    with pytest.raises(Conflict, match="run_seen"):
        store.record_rereview_run(repo="acme/scorecard-factory", run_id=11, outcome="event", now=T0)
    with pytest.raises(ValueError):
        store.record_rereview_run(repo="acme/notes", run_id=99, outcome="whatever", now=T0)
    assert store.rereview_run_counts() == {"event": 1, "invalid": 2}


def test_an_event_is_stored_once_per_repo_and_head_and_only_when_valid(store):
    first = store.add_rereview_event(doc=event_doc(), run_repo="acme/scorecard-factory", run_id=11, now=T0)
    assert (first.repo, first.head_sha, first.pr, first.level, first.signals[0]["id"]) == ("acme/notes", "a" * 40, 7, "elevated", "T-17")
    assert store.add_rereview_event(doc=event_doc(pr=8), run_repo="acme/scorecard-factory", run_id=12, now=T0) is None
    with pytest.raises(ValueError):
        store.add_rereview_event(doc={**event_doc(head="d" * 40), "level": "high"}, run_repo="acme/notes", run_id=13, now=T0)


def test_an_item_opens_once_per_key_one_at_a_time_and_is_audited(store):
    item = store.open_rereview_item(repo="acme/notes", reason="review_overdue", key="2026-10-01", now=T0)
    assert item.id.startswith("i_") and item.closed_at is None and item.events == ()
    with pytest.raises(Conflict, match="item_open"):
        store.open_rereview_item(repo="ACME/notes", reason="review_overdue", key="2026-10-02", now=T0)
    store.open_rereview_item(repo="acme/notes", reason="review_due_soon", key="2026-10-01", now=T0)  # another reason
    entry = store.audit_for("rereview_item", item.id)[0]
    assert (entry.action, entry.actor_kind, entry.after["reason"]) == ("rereview_item.opened", "system", "review_overdue")
    for reason, key, event in (("someday", "2026-10-01", None), ("review_overdue", "not-a-date", None),
                               ("scorecard_signal", "a" * 40, None), ("review_due_soon", "2026-10-01", ("a" * 40, ("T-17",)))):
        with pytest.raises(ValueError):
            store.open_rereview_item(repo="acme/tools", reason=reason, key=key, now=T0, event=event)


def test_a_scorecard_item_collects_later_events_while_it_is_open(store):
    for head in ("a" * 40, "d" * 40):
        store.add_rereview_event(doc=event_doc(head=head), run_repo="acme/scorecard-factory", run_id=int(head[0], 16), now=T0)
    with pytest.raises(Conflict, match="no_event"):
        store.open_rereview_item(repo="acme/notes", reason="scorecard_signal", key="e" * 40, now=T0, event=("e" * 40, ("T-17",)))
    item = store.open_rereview_item(repo="acme/notes", reason="scorecard_signal", key="a" * 40, now=T0, event=("a" * 40, ("T-17",)))
    item = store.add_rereview_item_event(item_id=item.id, head_sha="d" * 40, matched=("T-17",), now=T0 + HOUR)
    assert [(e.event.head_sha, e.matched) for e in item.events] == [("a" * 40, ("T-17",)), ("d" * 40, ("T-17",))]
    with pytest.raises(Conflict, match="event_exists"):
        store.add_rereview_item_event(item_id=item.id, head_sha="d" * 40, matched=("T-17",), now=T0)
    assert [i.id for i in store.rereview_items()] == [item.id] and store.rereview_items()[0].events == item.events


def test_closing_needs_a_note_is_audited_and_is_never_the_owner_s(store):
    owner, admin = people(store)
    store.replace_intake_observed([observed(owner=owner.id)], observed_at=T0)
    item = store.open_rereview_item(repo="acme/notes", reason="review_overdue", key="2026-10-01", now=T0)
    for note in ("short", "x" * 1001, "two\nlines here"):
        with pytest.raises(ValueError):
            store.close_rereview_item(item_id=item.id, actor_id=admin.id, note=note, now=T0, events_seen=0)
    with pytest.raises(Conflict, match="own_app"):
        store.close_rereview_item(item_id=item.id, actor_id=owner.id, note="Renewal requested by the owner.", now=T0, events_seen=0)
    closed = store.close_rereview_item(item_id=item.id, actor_id=admin.id, note="Owner renewing; tracked in r_abc.", now=T0 + HOUR, events_seen=0)
    assert (closed.close_kind, closed.closed_by, closed.close_note) == ("person", admin.id, "Owner renewing; tracked in r_abc.")
    assert closed.closed_at is not None and store.rereview_items() == [] and [i.id for i in store.rereview_items(open_only=False)] == [item.id]
    last = store.audit_for("rereview_item", item.id)[-1]
    assert (last.action, last.actor_kind, last.actor_id, last.reason) == ("rereview_item.closed", "person", admin.id, closed.close_note)
    with pytest.raises(Conflict, match="closed"):
        store.close_rereview_item(item_id=item.id, actor_id=admin.id, note="Closing it a second time.", now=T0, events_seen=0)
    with pytest.raises(Conflict, match="not_found"):
        store.close_rereview_item(item_id="i_" + "0" * 32, actor_id=admin.id, note="There is no such item.", now=T0, events_seen=0)


def test_a_closed_item_never_reopens_and_the_platform_closes_date_items_by_itself(store):
    item = store.open_rereview_item(repo="acme/notes", reason="review_due_soon", key="2026-11-01", now=T0)
    closed = store.auto_close_rereview_item(item_id=item.id, note="The record's review date is now 2027-11-01.", now=T0)
    assert (closed.close_kind, closed.closed_by) == ("auto", None)
    assert store.audit_for("rereview_item", item.id)[-1].actor_kind == "system"
    with pytest.raises(Conflict, match="item_exists"):
        store.open_rereview_item(repo="acme/notes", reason="review_due_soon", key="2026-11-01", now=T0)
    store.open_rereview_item(repo="acme/notes", reason="review_due_soon", key="2027-11-01", now=T0)  # a new date is a new item


def test_each_source_keeps_its_last_status(store):
    assert store.rereview_status() == {}
    store.set_rereview_status(source="events", problem="actions_unreadable", counts={}, now=T0)
    store.set_rereview_status(source="events", problem=None, counts={"event": 2}, now=T0 + HOUR)
    store.set_rereview_status(source="intake", problem=None, counts={"records": 3}, now=T0)
    status = store.rereview_status()
    assert (status["events"].problem, status["events"].counts, status["intake"].counts) == (None, {"event": 2}, {"records": 3})
    for source, problem in (("other", None), ("intake", "actions_unreadable")):
        with pytest.raises(ValueError):
            store.set_rereview_status(source=source, problem=problem, counts={}, now=T0)


def test_an_approval_must_keep_the_scorecard_mark_the_evidence_saw(store):
    owner, admin = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    evidence = give_evidence(store, req, record_sha="c" * 40, record={**RECORD, "scorecard": "central"})
    with pytest.raises(Conflict, match="evidence_mismatch"):
        store.decide(request_id=req.id, actor_id=admin.id, actor_groups=[], approve=True, note="ok", final=final_for(evidence), now=T0)
    store.decide(request_id=req.id, actor_id=admin.id, actor_groups=[], approve=True, note="ok",
                 final=final_for(evidence, scorecard="central"), now=T0)


# ---- the same refusals in both stores ----

@pytest.mark.parametrize("changes", [{"tier": "huge"}, {"data_classification": "internal"}, {"purpose": "x" * 4001}, {"by_portal": 1},
                                     {"by_portal": None}])
def test_an_observed_row_names_known_levels_a_bounded_purpose_and_a_real_flag(store, changes):
    with pytest.raises(ValueError):
        store.replace_intake_observed([observed(**changes)], observed_at=T0)
    store.replace_intake_observed([observed(purpose="x" * 4000)], observed_at=T0)  # the bound itself is fine


def test_an_item_s_event_matches_at_most_40_signals(store):
    store.add_rereview_event(doc=event_doc(), run_repo="acme/scorecard-factory", run_id=11, now=T0)
    with pytest.raises(ValueError):
        store.open_rereview_item(repo="acme/notes", reason="scorecard_signal", key="a" * 40, now=T0, event=("a" * 40, ("T-17",) * 41))
    store.open_rereview_item(repo="acme/notes", reason="scorecard_signal", key="a" * 40, now=T0, event=("a" * 40, ("T-17",) * 40))


def test_a_close_names_a_known_person(store):
    item = store.open_rereview_item(repo="acme/notes", reason="review_overdue", key="2026-10-01", now=T0)
    for actor in (None, "p_" + "0" * 32):
        with pytest.raises(Conflict, match="unknown_person"):
            store.close_rereview_item(item_id=item.id, actor_id=actor, note="Closing for nobody at all.", now=T0, events_seen=0)
    assert store.rereview_items()[0].closed_at is None


def test_the_platform_never_closes_a_scorecard_item_even_one_already_closed(store):
    _, admin = people(store)
    store.add_rereview_event(doc=event_doc(), run_repo="acme/scorecard-factory", run_id=11, now=T0)
    item = store.open_rereview_item(repo="acme/notes", reason="scorecard_signal", key="a" * 40, now=T0, event=("a" * 40, ("T-17",)))
    store.close_rereview_item(item_id=item.id, actor_id=admin.id, note="Reviewed the T-17 change.", now=T0, events_seen=1)
    with pytest.raises(Conflict, match="wrong_reason"):  # the reason is checked before the state, in every store
        store.auto_close_rereview_item(item_id=item.id, note="The date moved, they say.", now=T0)


def test_a_close_names_the_events_the_admin_saw(store):
    _, admin = people(store)
    for head, run_id in (("a" * 40, 11), ("d" * 40, 12)):
        store.add_rereview_event(doc=event_doc(head=head), run_repo="acme/scorecard-factory", run_id=run_id, now=T0)
    item = store.open_rereview_item(repo="acme/notes", reason="scorecard_signal", key="a" * 40, now=T0, event=("a" * 40, ("T-17",)))
    store.add_rereview_item_event(item_id=item.id, head_sha="d" * 40, matched=("T-17",), now=T0)  # after the admin looked
    with pytest.raises(Conflict, match="item_changed"):
        store.close_rereview_item(item_id=item.id, actor_id=admin.id, note="Reviewed the T-17 change.", now=T0, events_seen=1)
    store.close_rereview_item(item_id=item.id, actor_id=admin.id, note="Reviewed both T-17 changes.", now=T0, events_seen=2)


def test_a_record_under_two_spellings_is_observed_as_a_problem_with_no_view(store):
    store.replace_intake_observed([observed(problem="case_mismatch", schema=None, owner=None, tier=None, data_classification=None,
                                            purpose=None, review_by=None, by_portal=False)], observed_at=T0)
    assert store.intake_observed()[0].problem == "case_mismatch"


# ---- provenance, the run watermark, an interrupted pass ----

def test_an_untrusted_run_is_an_outcome(store):
    store.record_rereview_run(repo="acme/scorecard-factory", run_id=5, outcome="untrusted_run", now=T0)
    assert store.rereview_run_counts() == {"untrusted_run": 1}


def test_a_stored_event_is_found_by_its_repo_in_any_case_and_head(store):
    store.add_rereview_event(doc=event_doc(), run_repo="acme/scorecard-factory", run_id=11, now=T0)
    found = store.rereview_event("ACME/notes", "a" * 40)
    assert (found.run_repo, found.run_id, found.head_sha) == ("acme/scorecard-factory", 11, "a" * 40)
    assert store.rereview_event("acme/notes", "d" * 40) is None


def test_each_source_s_watermark_only_rises(store):
    assert store.rereview_watermark("acme/scorecard-factory") is None
    store.set_rereview_watermark(repo="acme/scorecard-factory", run_id=100, now=T0)
    store.set_rereview_watermark(repo="ACME/scorecard-factory", run_id=40, now=T0)   # lower: kept at 100
    assert store.rereview_watermark("acme/Scorecard-Factory") == 100 and store.rereview_watermark("acme/notes") is None
    store.set_rereview_watermark(repo="acme/scorecard-factory", run_id=150, now=T0)
    assert store.rereview_watermark("acme/scorecard-factory") == 150
    for repo, run_id in (("acme", 5), ("acme/notes", 0), ("acme/notes", "5")):
        with pytest.raises(ValueError):
            store.set_rereview_watermark(repo=repo, run_id=run_id, now=T0)


# ---- failed runs are re-checked until a later attempt succeeds or their artifacts are gone ----

def test_a_failed_run_is_kept_with_its_attempt_until_it_is_dropped(store):
    store.record_failed_run(repo="acme/scorecard-factory", run_id=7, attempt=1, last_attempt_at=T0, retention_days=90, now=T0)
    store.record_failed_run(repo="ACME/scorecard-factory", run_id=7, attempt=2, last_attempt_at=T0 + HOUR, retention_days=90, now=T0)
    [run] = store.failed_runs("acme/Scorecard-Factory")
    assert (run.run_id, run.attempt, run.last_attempt_at, run.retention_days) == (7, 2, T0 + HOUR, 90)
    assert store.failed_runs("acme/notes") == []
    store.drop_failed_run(repo="acme/scorecard-factory", run_id=7)
    assert store.failed_runs("acme/scorecard-factory") == []
    for bad in ({"attempt": 0}, {"retention_days": 0}, {"retention_days": 401}, {"run_id": -1}, {"repo": "acme"}):
        with pytest.raises(ValueError):
            store.record_failed_run(**{"repo": "acme/notes", "run_id": 8, "attempt": 1, "last_attempt_at": T0, "retention_days": 90, "now": T0, **bad})


# ---- events per source, events waiting for their app, each collector part's status ----

def test_an_event_is_stored_once_per_source_so_a_per_app_one_never_hides_a_central_one(store):
    assert store.add_rereview_event(doc=event_doc(), run_repo="acme/notes", run_id=3, now=T0, source="per-app") is not None
    central = store.add_rereview_event(doc=event_doc(), run_repo="acme/scorecard-factory", run_id=11, now=T0)
    assert central is not None and central.source == "central"
    assert store.add_rereview_event(doc=event_doc(pr=9), run_repo="acme/scorecard-factory", run_id=12, now=T0) is None
    assert (store.rereview_event("acme/notes", "a" * 40).run_id, store.rereview_event("acme/notes", "a" * 40, "per-app").run_id) == (11, 3)
    item = store.open_rereview_item(repo="acme/notes", reason="scorecard_signal", key="a" * 40, now=T0, event=("a" * 40, ("T-17",), "per-app"))
    item = store.add_rereview_item_event(item_id=item.id, head_sha="a" * 40, matched=("T-17",), now=T0, source="central")
    assert [(e.event.source, e.event.run_id) for e in item.events] == [("per-app", 3), ("central", 11)]
    with pytest.raises(ValueError):
        store.add_rereview_event(doc=event_doc(head="d" * 40), run_repo="acme/notes", run_id=4, now=T0, source="elsewhere")


def test_an_event_waits_for_its_app_s_record_without_holding_anything_else(store):
    store.hold_pending_event(source="central", run_repo="acme/scorecard-factory", run_id=21, doc=event_doc(repo="acme/later"), now=T0)
    store.hold_pending_event(source="central", run_repo="ACME/scorecard-factory", run_id=21, doc=event_doc(repo="acme/later"), now=T0)  # once
    [pending] = store.pending_events()
    assert (pending.source, pending.run_repo, pending.run_id, pending.doc["repo"]) == ("central", "acme/scorecard-factory", 21, "acme/later")
    store.drop_pending_event(run_repo="acme/scorecard-factory", run_id=21)
    assert store.pending_events() == []
    with pytest.raises(ValueError):
        store.hold_pending_event(source="central", run_repo="acme/scorecard-factory", run_id=22, doc={**event_doc(), "level": "high"}, now=T0)


def test_each_collector_part_keeps_when_it_last_worked(store):
    store.set_rereview_status(source="repo_admins", problem=None, counts={"interval_seconds": 300}, now=T0)
    store.set_rereview_status(source="repo_admins", problem="failed", counts={}, now=T0 + HOUR)
    status = store.rereview_status()["repo_admins"]
    assert (status.problem, status.last_ok_at is not None) == ("failed", True)
    store.set_rereview_status(source="events", problem="factory_not_main", counts={}, now=T0)
    store.set_rereview_status(source="evidence", problem=None, counts={}, now=T0)
    with pytest.raises(ValueError):
        store.set_rereview_status(source="evidence", problem="actions_unreadable", counts={}, now=T0)


def test_the_watermarks_are_listed_for_the_page(store):
    store.set_rereview_watermark(repo="acme/scorecard-factory", run_id=40, now=T0)
    [mark] = store.rereview_watermarks()
    assert (mark.repo, mark.run_id) == ("acme/scorecard-factory", 40) and mark.updated_at is not None


# ---- final review: a failed run's re-check times, every failed run, a run stuck on GitHub being busy ----

def test_a_failed_run_knows_when_it_was_first_seen_and_last_checked(store):
    store.record_failed_run(repo="acme/notes", run_id=7, attempt=1, last_attempt_at=T0, retention_days=90, now=T0)
    store.record_failed_run(repo="acme/notes", run_id=7, attempt=1, last_attempt_at=T0, retention_days=90, now=T0 + HOUR)
    store.record_failed_run(repo="acme/tools", run_id=8, attempt=1, last_attempt_at=T0, retention_days=90, now=T0)
    [run] = store.failed_runs("acme/notes")
    assert run.seen_at is not None and run.checked_at is not None and run.checked_at >= run.seen_at
    assert sorted((f.repo, f.run_id) for f in store.failed_runs()) == [("acme/notes", 7), ("acme/tools", 8)]


def test_passes_stuck_on_one_run_are_counted_until_cleared(store):
    assert [store.note_stuck_run(repo="acme/scorecard-factory", run_id=5, now=T0) for _ in range(3)] == [1, 2, 3]
    assert store.stuck_runs("ACME/scorecard-factory") == {5: 3}
    store.clear_stuck_run(repo="acme/scorecard-factory", run_id=5)
    assert store.stuck_runs("acme/scorecard-factory") == {}


def test_a_walk_that_ends_at_the_same_run_notes_when_it_did(store):
    store.set_rereview_watermark(repo="acme/scorecard-factory", run_id=40, now=T0)
    first = store.rereview_watermarks()[0].updated_at
    store.set_rereview_watermark(repo="acme/scorecard-factory", run_id=40, now=T0 + HOUR)
    assert store.rereview_watermarks()[0].updated_at > first


def test_a_close_is_refused_while_the_record_can_t_be_read_and_by_whoever_last_registered_the_app(store):
    owner, admin = people(store)
    store.replace_intake_observed([observed(problem="invalid", schema=None, owner=None, tier=None, data_classification=None, purpose=None,
                                            review_by=None, by_portal=False)], observed_at=T0)
    item = store.open_rereview_item(repo="acme/notes", reason="review_overdue", key="2026-10-01", now=T0)
    with pytest.raises(Conflict, match="record_unreadable"):
        store.close_rereview_item(item_id=item.id, actor_id=admin.id, note="Closing it while it is broken.", now=T0, events_seen=0)
    store.replace_intake_observed([observed(owner="p_" + "9" * 32)], observed_at=T0)   # the record names someone else
    req = store.create_registration(person_id=admin.id, draft=DRAFT, now=T0)            # but the admin's change was approved last
    evidence = give_evidence(store, req)
    store.decide(request_id=req.id, actor_id=owner.id, actor_groups=[], approve=True, note="ok", final=final_for(evidence), now=T0)
    with pytest.raises(Conflict, match="own_app"):
        store.close_rereview_item(item_id=item.id, actor_id=admin.id, note="Closing my own app's item.", now=T0, events_seen=0)
    store.close_rereview_item(item_id=item.id, actor_id=owner.id, note="Reviewed the change with security.", now=T0, events_seen=0)


# ---- 0.6.0: the CMDB record, the ownership acknowledgement, the cmdb part (spec 2026-10-31 Part 3) ----

CMDB_REPO = "acme/notes"


def test_a_cmdb_sync_is_recorded_and_an_error_keeps_the_ci(store):
    assert store.cmdb_records() == [] and store.cmdb_record(CMDB_REPO) is None
    store.record_cmdb_sync(repo=CMDB_REPO, sys_id="c" * 32, number="APM0001001", fields_hash="f" * 64, operation="INSERT", now=T0)
    first = store.cmdb_record("ACME/notes")
    assert (first.repo, first.sys_id, first.number, first.last_operation, first.last_error) == (CMDB_REPO, "c" * 32, "APM0001001", "INSERT", None)
    assert first.last_synced_at is not None and first.last_error_at is None
    store.record_cmdb_error(repo=CMDB_REPO, error="cmdb_unreachable", now=T0 + HOUR)
    kept = store.cmdb_record(CMDB_REPO)
    assert (kept.number, kept.fields_hash, kept.last_operation, kept.last_error) == ("APM0001001", "f" * 64, "INSERT", "cmdb_unreachable")
    assert kept.last_error_at and kept.last_synced_at == first.last_synced_at
    store.record_cmdb_sync(repo=CMDB_REPO, sys_id="c" * 32, number="APM0001001", fields_hash="e" * 64, operation="UPDATE", now=T0 + 2 * HOUR)
    again = store.cmdb_record(CMDB_REPO)
    assert (again.last_error, again.last_error_at, again.fields_hash, again.last_operation) == (None, None, "e" * 64, "UPDATE")
    assert [r.repo for r in store.cmdb_records()] == [CMDB_REPO]


def test_an_owner_unknown_before_any_sync_has_no_ci(store):
    store.record_cmdb_error(repo=CMDB_REPO, error="cmdb_owner_unknown", now=T0)
    r = store.cmdb_record(CMDB_REPO)
    assert (r.sys_id, r.number, r.fields_hash, r.last_synced_at, r.last_error) == (None, None, None, None, "cmdb_owner_unknown")
    store.record_cmdb_error(repo="ACME/notes", error="cmdb_refused", now=T0)   # the same record, in any case
    assert [x.last_error for x in store.cmdb_records()] == ["cmdb_refused"]


def test_cmdb_records_are_listed_by_repo_in_any_case(store):
    for repo in ("acme/Zeta", "acme/alpha", "Acme/beta"):
        store.record_cmdb_error(repo=repo, error="cmdb_owner_unknown", now=T0)
    assert [r.repo for r in store.cmdb_records()] == ["acme/alpha", "Acme/beta", "acme/Zeta"]


@pytest.mark.parametrize("bad", [dict(sys_id="x"), dict(number="apm1"), dict(fields_hash="f"), dict(operation="DELETE"), dict(repo="x"),
                                 dict(sys_id=None), dict(number=None)])
def test_a_malformed_sync_is_refused(store, bad):
    args = dict(repo=CMDB_REPO, sys_id="c" * 32, number="APM0001001", fields_hash="f" * 64, operation="INSERT", now=T0)
    args.update(bad)
    with pytest.raises(ValueError):
        store.record_cmdb_sync(**args)
    assert store.cmdb_records() == []


@pytest.mark.parametrize("repo, error", [("x", "cmdb_unreachable"), (CMDB_REPO, "failed"), (CMDB_REPO, None)])
def test_a_malformed_cmdb_error_is_refused(store, repo, error):
    with pytest.raises(ValueError):
        store.record_cmdb_error(repo=repo, error=error, now=T0)


def test_a_registration_records_its_acknowledgement(store):
    owner, _ = people(store)
    draft = {"schema": "registration-draft/v1", "repo": "acme/other", "purpose": "Stores short team notes; no email, no files.",
             "data_classification": "public"}
    req = store.create_registration(person_id=owner.id, draft=draft, now=T0, ownership_acknowledged_at=T0)
    # the memory store keeps the time it is given; Postgres sets it to its own now() (0011's insert guard): either way it is recorded
    assert req.ownership_acknowledged_at is not None and store.get_request(req.id).ownership_acknowledged_at == req.ownership_acknowledged_at
    old = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    assert store.get_request(old.id).ownership_acknowledged_at is None   # as every request before 0.6.0


def test_a_resubmit_keeps_the_acknowledgement(store):
    owner, admin = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0, ownership_acknowledged_at=T0)
    store.decide(request_id=req.id, actor_id=admin.id, actor_groups=["golden-road-admins"], approve=False, note="needs more detail",
                 final=None, now=T0)
    store.resubmit(request_id=req.id, person_id=owner.id, draft=DRAFT, now=T0 + HOUR)
    assert req.ownership_acknowledged_at is not None and store.get_request(req.id).ownership_acknowledged_at == req.ownership_acknowledged_at


def test_the_owner_s_email_is_readable(store):
    owner, _ = people(store)
    assert store.person_email(owner.id) == owner.email == "sub-owner@example.test" and store.person_email("p_" + "0" * 32) is None


def test_the_cmdb_part_has_its_own_problems(store):
    store.set_rereview_status(source="cmdb", problem="cmdb_owner_unknown", counts={"owner_unknown": 1}, now=T0)
    assert store.rereview_status()["cmdb"].problem == "cmdb_owner_unknown"
    for problem in ("cmdb_unreachable", "cmdb_refused", "failed", None):
        store.set_rereview_status(source="cmdb", problem=problem, counts={}, now=T0)
    with pytest.raises(ValueError):
        store.set_rereview_status(source="cmdb", problem="factory_unreadable", counts={}, now=T0)
    with pytest.raises(ValueError):
        store.set_rereview_status(source="intake", problem="cmdb_unreachable", counts={}, now=T0)


from portal.store.base import ACTION_STEPS  # noqa: E402

LIFE = {"action": "suspend", "task": "SCTASK0010001", "task_sys_id": "e" * 32, "reason": "Critical vulnerability; suspended until patched"}
NOTE = "Requested in the golden road portal by Admin: suspend the app. Reason: Critical vulnerability; suspended until patched"


def lifecycle_request(store, admin, action="suspend", repo="acme/notes", rid=None):
    from portal.ids import new_id
    rid = rid or new_id("r")
    return store.create_lifecycle(request_id=rid, person_id=admin.id, repo=repo, final={**LIFE, "action": action}, note=NOTE, now=T0)


def test_a_lifecycle_request_starts_approved_with_its_first_note(store):
    _, admin = people(store)
    req = lifecycle_request(store, admin)
    assert (req.kind, req.state, req.final, req.draft, req.requested_by) == ("lifecycle", "approved", {**LIFE}, {**LIFE}, admin.id)
    [event] = store.lifecycle_events(req.id)
    assert (event.step, event.note, event.task_sys_id, event.sent_at, event.attempts) == ("requested", NOTE, "e" * 32, None, 0)
    created = [e for e in store.audit_for("request", req.id) if e.action == "request.created"][0]
    assert created.reason == LIFE["reason"] and created.after["final"] == LIFE


def test_one_lifecycle_request_open_per_app(store):
    _, admin = people(store)
    lifecycle_request(store, admin)
    with pytest.raises(Conflict) as err:
        lifecycle_request(store, admin, action="decommission", repo="ACME/notes")
    assert err.value.code == "open_request_exists"
    store.create_registration(person_id=admin.id, draft={**DRAFT, "repo": "acme/other"}, now=T0)  # other kinds are not held


COMPLETED = "Suspended in git: every environment of acme/notes scales to zero. The task is closed."
PR = dict(branch="portal/lifecycle-x-deploy-1", pr_number=7, pr_url="https://github.com/acme/golden-road-deploy/pull/7", head_sha="2" * 40,
          base_sha="1" * 40)


def writing_lifecycle(store, admin, action="suspend", repo="acme/notes"):
    """A lifecycle request the writer claimed and planned: (request, its steps)."""
    req = lifecycle_request(store, admin, action=action, repo=repo)
    assert store.claim_approved(now=T0).id == req.id
    return req, store.add_lifecycle_steps(request_id=req.id, steps=ACTION_STEPS[action], now=T0)


def every_step_done(store, req):
    for s in store.lifecycle_steps(req.id):
        store.move_lifecycle_step(request_id=req.id, step=s.step, to_state="done", now=T0)


def test_a_lifecycle_request_is_applied_or_abandoned(store):
    _, admin = people(store)
    req, steps = writing_lifecycle(store, admin)
    assert [(s.step, s.position, s.state, s.attempt) for s in steps] == [("deploy", 1, "planned", 1)]
    store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="pr_open", now=T0, **PR)
    done = store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="done", now=T0 + HOUR, merge_sha="3" * 40)
    assert (done.state, done.merge_sha, done.pr_number) == ("done", "3" * 40, 7)
    applied = store.apply_lifecycle(request_id=req.id, merge_sha="3" * 40, note=COMPLETED, now=T0 + HOUR)
    assert (applied.state, applied.merge_sha) == ("merged", "3" * 40)
    state = store.lifecycle_state("acme/notes")
    assert (state.state, state.task, state.request) == ("suspended", "SCTASK0010001", req.id)
    assert [(e.step, e.note) for e in store.lifecycle_events(req.id)][-1] == ("completed", COMPLETED)
    other = lifecycle_request(store, admin, action="resume")  # applied: a new one may open
    store.claim_approved(now=T0)
    assert store.mark_failed(request_id=other.id, reason="abandoned", now=T0).state == "failed"


def test_a_step_moves_only_by_its_transitions(store):
    _, admin = people(store)
    req, _ = writing_lifecycle(store, admin, action="decommission")
    with pytest.raises(Conflict) as err:
        store.move_lifecycle_step(request_id=req.id, step="factory", to_state="planned", now=T0)  # the deploy step isn't done
    assert err.value.code == "wrong_state"
    rebuilt = store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="planned", now=T0)  # a stranded branch: a new attempt
    assert (rebuilt.state, rebuilt.attempt) == ("planned", 2)
    failed = store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="failed", now=T0, failure_reason="check_failed: deploy-check")
    assert failed.failure_reason == "check_failed: deploy-check"
    again = store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="planned", now=T0)
    assert (again.state, again.attempt, again.failure_reason, again.pr_number) == ("planned", 3, None, None)
    store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="closed", now=T0)
    with pytest.raises(Conflict):
        store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="planned", now=T0)  # closed is for good
    with pytest.raises(ValueError):
        store.move_lifecycle_step(request_id=req.id, step="factory", to_state="pr_open", now=T0, branch="b")  # a PR names all of it


def test_a_decommission_s_steps_go_in_rule_7_s_order(store):
    """deploy, then archive, then factory (decided by the lead while Mike was away): a step leaves planned only once every earlier one
    is done, but an abandon closes any of them."""
    _, admin = people(store)
    req, steps = writing_lifecycle(store, admin, action="decommission")
    assert [(s.step, s.position) for s in steps] == [("deploy", 1), ("archive", 2), ("factory", 3)]
    for step, to_state, fields in (("archive", "done", {}), ("archive", "failed", {"failure_reason": "archive: github: 404"}),
                                   ("factory", "pr_open", PR)):
        with pytest.raises(Conflict) as err:
            store.move_lifecycle_step(request_id=req.id, step=step, to_state=to_state, now=T0, **fields)
        assert err.value.code == "wrong_state"
    store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="done", now=T0, merge_sha="3" * 40)
    with pytest.raises(Conflict):
        store.move_lifecycle_step(request_id=req.id, step="factory", to_state="pr_open", now=T0, **PR)  # the archive isn't done
    store.move_lifecycle_step(request_id=req.id, step="archive", to_state="done", now=T0)
    assert store.move_lifecycle_step(request_id=req.id, step="factory", to_state="pr_open", now=T0, **PR).state == "pr_open"
    other, _ = writing_lifecycle(store, admin, action="decommission", repo="acme/tools")
    for step in ("factory", "archive", "deploy"):  # an abandon closes every step, in any order
        assert store.move_lifecycle_step(request_id=other.id, step=step, to_state="closed", now=T0).state == "closed"


def test_a_step_is_planned_and_moved_only_while_its_request_is_being_written(store):
    _, admin = people(store)
    req = lifecycle_request(store, admin)
    with pytest.raises(Conflict) as err:  # not claimed yet
        store.add_lifecycle_steps(request_id=req.id, steps=ACTION_STEPS["suspend"], now=T0)
    assert err.value.code == "wrong_state"
    store.claim_approved(now=T0)
    store.add_lifecycle_steps(request_id=req.id, steps=ACTION_STEPS["suspend"], now=T0)
    store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="failed", now=T0, failure_reason="check_failed: deploy-check")
    store.mark_failed(request_id=req.id, reason="abandoned", now=T0)
    for to_state in ("planned", "closed"):
        with pytest.raises(Conflict) as err:
            store.move_lifecycle_step(request_id=req.id, step="deploy", to_state=to_state, now=T0)
        assert err.value.code == "wrong_state"


def test_an_open_step_s_pull_request_never_changes(store):
    _, admin = people(store)
    req, _ = writing_lifecycle(store, admin)
    store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="pr_open", now=T0, **PR)
    for to_state, fields in (("done", {"head_sha": "4" * 40}), ("failed", {"failure_reason": "x", "pr_number": 8}), ("closed", {"branch": "b"})):
        with pytest.raises(ValueError):
            store.move_lifecycle_step(request_id=req.id, step="deploy", to_state=to_state, now=T0, **fields)
    done = store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="done", now=T0, merge_sha="3" * 40)
    assert {f: getattr(done, f) for f in PR} == PR


def test_the_state_is_the_one_its_request_applies(store):
    """Repo and task come from the request: the writer names only the request and the state its action applies."""
    _, admin = people(store)
    req, _ = writing_lifecycle(store, admin)
    assert store.lifecycle_state("acme/notes") is None and store.lifecycle_states() == []
    for state in ("active", "decommissioned"):  # a suspend applies suspended
        with pytest.raises(Conflict) as err:
            store.set_lifecycle_state(request_id=req.id, state=state, now=T0)
        assert err.value.code == "wrong_state"
    with pytest.raises(ValueError):
        store.set_lifecycle_state(request_id=req.id, state="paused", now=T0)
    found = store.set_lifecycle_state(request_id=req.id, state="suspended", now=T0)
    assert found == store.lifecycle_state("ACME/Notes")
    assert (found.repo, found.state, found.task, found.request) == ("acme/notes", "suspended", "SCTASK0010001", req.id)
    audits = len(store.audit_for("app", "acme/notes"))
    assert store.set_lifecycle_state(request_id=req.id, state="suspended", now=T0) == found  # again: no change, no new entry
    assert len(store.audit_for("app", "acme/notes")) == audits == 1
    queued = lifecycle_request(store, admin, repo="acme/tools")  # approved, not claimed
    with pytest.raises(Conflict) as err:
        store.set_lifecycle_state(request_id=queued.id, state="suspended", now=T0)
    assert err.value.code == "wrong_state"
    with pytest.raises(Conflict) as err:
        store.set_lifecycle_state(request_id="r_" + "0" * 32, state="suspended", now=T0)
    assert err.value.code == "not_found"


def test_the_state_follows_the_applied_action_and_decommissioned_is_for_good(store):
    _, admin = people(store)
    for action, state, task in (("suspend", "suspended", "SCTASK0010001"), ("resume", "active", None),
                                ("decommission", "decommissioned", "SCTASK0010001")):
        req, _ = writing_lifecycle(store, admin, action=action)
        every_step_done(store, req)
        store.apply_lifecycle(request_id=req.id, merge_sha=None, note=COMPLETED, now=T0)
        found = store.lifecycle_state("acme/notes")
        assert (found.state, found.task, found.request) == (state, task, req.id)
    with pytest.raises(Conflict) as err:
        lifecycle_request(store, admin, action="resume")
    assert err.value.code == "decommissioned"


def test_apply_sets_the_state_notes_completed_and_marks_the_request_merged_at_once(store):
    _, admin = people(store)
    req, _ = writing_lifecycle(store, admin, action="decommission")
    store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="done", now=T0, merge_sha="3" * 40)
    with pytest.raises(Conflict) as err:  # the archive and the factory aren't done
        store.apply_lifecycle(request_id=req.id, merge_sha="3" * 40, note=COMPLETED, now=T0)
    assert err.value.code == "wrong_state"
    store.set_lifecycle_state(request_id=req.id, state="decommissioned", now=T0)  # the deploy step's own (rule 4 from then on)
    store.move_lifecycle_step(request_id=req.id, step="archive", to_state="done", now=T0)
    store.move_lifecycle_step(request_id=req.id, step="factory", to_state="done", now=T0, merge_sha="4" * 40)
    applied = store.apply_lifecycle(request_id=req.id, merge_sha="4" * 40, note=COMPLETED, now=T0)  # the state set already: kept
    assert (applied.state, applied.merge_sha, store.lifecycle_state("acme/notes").request) == ("merged", "4" * 40, req.id)
    with pytest.raises(Conflict) as err:
        store.apply_lifecycle(request_id=req.id, merge_sha="4" * 40, note=COMPLETED, now=T0)
    assert err.value.code == "wrong_state"
    other = lifecycle_request(store, admin, repo="acme/tools")
    store.claim_approved(now=T0)
    with pytest.raises(Conflict) as err:  # never applied without its steps (one refused before any is failed, not merged)
        store.apply_lifecycle(request_id=other.id, merge_sha=None, note=COMPLETED, now=T0)
    assert err.value.code == "wrong_state"


def test_a_failed_apply_changes_nothing(store):
    """One transaction: a request whose completed note is already there (as only a hand or a race could make it) is refused, and
    neither its state nor the request moved."""
    _, admin = people(store)
    req, _ = writing_lifecycle(store, admin)
    every_step_done(store, req)
    store.add_lifecycle_event(request_id=req.id, step="completed", note=COMPLETED, now=T0)
    with pytest.raises(Conflict) as err:
        store.apply_lifecycle(request_id=req.id, merge_sha=None, note=COMPLETED, now=T0)
    assert err.value.code == "event_exists"
    assert store.lifecycle_state("acme/notes") is None and store.get_request(req.id).state == "writing"
    assert not [e for e in store.audit_for("request", req.id) if e.action == "request.merged"]


@pytest.mark.parametrize("kind", ["registration", "lifecycle"])
def test_after_decommission_no_request_is_taken_for_the_repo(store, kind):
    """Rule 4 (decided while planning: item 15)."""
    _, admin = people(store)
    req, _ = writing_lifecycle(store, admin, action="decommission")
    store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="done", now=T0, merge_sha="3" * 40)
    store.set_lifecycle_state(request_id=req.id, state="decommissioned", now=T0)
    with pytest.raises(Conflict) as err:
        if kind == "registration":
            store.create_registration(person_id=admin.id, draft={**DRAFT, "repo": "Acme/Notes"}, now=T0)
        else:
            lifecycle_request(store, admin, action="resume")
    assert err.value.code == "decommissioned"


def test_commands_one_open_at_a_time(store):
    owner, admin = people(store)
    req = lifecycle_request(store, admin)
    command = store.add_lifecycle_command(request_id=req.id, command="retry", actor_id=admin.id, now=T0)
    assert store.open_lifecycle_command(req.id) == command and command.requested_by == admin.id
    with pytest.raises(Conflict) as err:
        store.add_lifecycle_command(request_id=req.id, command="abandon", actor_id=admin.id, now=T0)
    assert err.value.code == "command_open"
    store.finish_lifecycle_command(command_id=command.id, now=T0)
    assert store.open_lifecycle_command(req.id) is None
    assert store.add_lifecycle_command(request_id=req.id, command="abandon", actor_id=admin.id, now=T0).command == "abandon"
    with pytest.raises(ValueError):
        store.add_lifecycle_command(request_id=req.id, command="merge", actor_id=admin.id, now=T0)
    registration = store.create_registration(person_id=owner.id, draft={**DRAFT, "repo": "acme/tools"}, now=T0)
    with pytest.raises(Conflict) as err:
        store.add_lifecycle_command(request_id=registration.id, command="retry", actor_id=admin.id, now=T0)
    assert err.value.code == "not_lifecycle"


def test_the_outbox_keeps_order_attempts_and_errors(store):
    _, admin = people(store)
    req = lifecycle_request(store, admin)
    second = store.add_lifecycle_event(request_id=req.id, step="pr_opened", note="Pull request #7 opened.", now=T0)
    first = store.lifecycle_events(req.id)[0]
    assert [e.id for e in store.lifecycle_events(unsent=True)] == [first.id, second.id]
    store.mark_lifecycle_event_failed(event_id=first.id, error="ServiceNow answered 503 for the task's work note", now=T0)
    store.mark_lifecycle_event_failed(event_id=first.id, error="ServiceNow answered 503 for the task's work note", now=T0)
    kept = store.lifecycle_events(req.id)[0]
    assert (kept.attempts, kept.last_error, kept.sent_at) == (2, "ServiceNow answered 503 for the task's work note", None)
    store.mark_lifecycle_event_sent(event_id=first.id, now=T0 + HOUR)
    assert store.lifecycle_events(req.id)[0].sent_at is not None  # the database's time in Postgres (0012)
    assert [e.id for e in store.lifecycle_events(unsent=True)] == [second.id]
    with pytest.raises(Conflict):
        store.mark_lifecycle_event_sent(event_id=first.id, now=T0 + 2 * HOUR)  # sent once
    with pytest.raises(ValueError):
        store.add_lifecycle_event(request_id=req.id, step="paused", note="x", now=T0)


def test_a_lifecycle_request_keeps_its_pull_requests_on_its_steps(store):
    """0012's update guard: a lifecycle request never goes pr_open (its PRs are its steps'), and only it is applied without one."""
    _, admin = people(store)
    req = lifecycle_request(store, admin)
    store.claim_approved(now=T0)
    with pytest.raises(Conflict) as err:
        store.mark_pr_open(request_id=req.id, branch="portal/x", pr_number=7, pr_url="https://github.com/acme/golden-road-deploy/pull/7",
                           head_sha="2" * 40, base_sha="1" * 40, now=T0)
    assert err.value.code == "wrong_state"
    store.add_lifecycle_steps(request_id=req.id, steps=ACTION_STEPS["suspend"], now=T0)
    every_step_done(store, req)  # nothing to change in the deploy repo: no pull request at all
    assert store.apply_lifecycle(request_id=req.id, merge_sha=None, note=COMPLETED, now=T0).merge_sha is None
    registration = store.create_registration(person_id=admin.id, draft={**DRAFT, "repo": "acme/tools"}, now=T0)
    for call in (lambda: store.apply_lifecycle(request_id=registration.id, merge_sha=None, note=COMPLETED, now=T0),
                 lambda: store.set_lifecycle_state(request_id=registration.id, state="suspended", now=T0)):
        with pytest.raises(Conflict) as err:
            call()
        assert err.value.code == "not_lifecycle"


def test_a_request_s_notes_are_sent_in_order_and_unknown_ids_are_refused(store):
    _, admin = people(store)
    req = lifecycle_request(store, admin)
    first = store.lifecycle_events(req.id)[0]
    second = store.add_lifecycle_event(request_id=req.id, step="pr_opened", note="Pull request #7 opened.", now=T0)
    other = lifecycle_request(store, admin, repo="acme/tools")
    with pytest.raises(Conflict) as err:
        store.mark_lifecycle_event_sent(event_id=second.id, now=T0)  # the requested note is not sent yet
    assert err.value.code == "wrong_state"
    store.mark_lifecycle_event_sent(event_id=store.lifecycle_events(other.id)[0].id, now=T0)  # another request's notes don't wait
    store.mark_lifecycle_event_failed(event_id=second.id, error="ServiceNow answered 503 for the task's work note", now=T0)
    store.mark_lifecycle_event_sent(event_id=first.id, now=T0)
    store.mark_lifecycle_event_sent(event_id=second.id, now=T0)
    assert store.lifecycle_events(unsent=True) == []
    for missing in (0, -1, 10**6):
        for call in (lambda: store.mark_lifecycle_event_sent(event_id=missing, now=T0),
                     lambda: store.mark_lifecycle_event_failed(event_id=missing, error="ServiceNow answered 503", now=T0),
                     lambda: store.finish_lifecycle_command(command_id=missing, now=T0)):
            with pytest.raises(Conflict) as err:
                call()
            assert err.value.code == "wrong_state"
    assert [e.sent_at is not None for e in store.lifecycle_events(req.id)] == [True, True]


def test_a_request_has_one_requested_note(store):
    _, admin = people(store)
    req = lifecycle_request(store, admin)
    with pytest.raises(Conflict) as err:
        store.add_lifecycle_event(request_id=req.id, step="requested", note=NOTE, now=T0)
    assert err.value.code == "event_exists"
    assert [e.step for e in store.lifecycle_events(req.id)] == ["requested"]


def test_a_command_or_a_note_for_an_unknown_request_or_by_an_unknown_person_is_not_found(store):
    _, admin = people(store)
    req = lifecycle_request(store, admin)
    unknown = "r_" + "0" * 32
    for call in (lambda: store.add_lifecycle_command(request_id=unknown, command="retry", actor_id=admin.id, now=T0),
                 lambda: store.add_lifecycle_event(request_id=unknown, step="pr_opened", note="Pull request #7 opened.", now=T0),
                 lambda: store.add_lifecycle_command(request_id=req.id, command="retry", actor_id="p_" + "0" * 32, now=T0)):
        with pytest.raises(Conflict) as err:
            call()
        assert err.value.code == "not_found"
    assert store.open_lifecycle_command(req.id) is None


def test_a_step_moves_with_its_note_in_one_transaction(store):
    """L11 never loses a note: the writer moves a step and adds the note that says so together, or neither."""
    _, admin = people(store)
    req, _ = writing_lifecycle(store, admin)
    moved, event = store.move_lifecycle_step_with_event(request_id=req.id, step="deploy", to_state="pr_open", now=T0,
                                                        event="pr_opened", note="Pull request #7 opened.", **PR)
    assert (moved.state, moved.pr_number) == ("pr_open", 7) and store.lifecycle_steps(req.id)[0] == moved
    assert (event.step, event.note, event.request_id) == ("pr_opened", "Pull request #7 opened.", req.id)
    assert [e.step for e in store.lifecycle_events(req.id)] == ["requested", "pr_opened"]
    with pytest.raises(Conflict) as err:  # no move: no note
        store.move_lifecycle_step_with_event(request_id=req.id, step="deploy", to_state="pr_open", now=T0, event="pr_opened", note="Again.", **PR)
    assert err.value.code == "wrong_state"
    with pytest.raises(ValueError):  # no note: no move
        store.move_lifecycle_step_with_event(request_id=req.id, step="deploy", to_state="done", now=T0, event="paused", note="x", merge_sha="3" * 40)
    with pytest.raises(ValueError):
        store.move_lifecycle_step_with_event(request_id=req.id, step="deploy", to_state="done", now=T0, event="merged", note="", merge_sha="3" * 40)
    store.add_lifecycle_event(request_id=req.id, step="completed", note=COMPLETED, now=T0)
    with pytest.raises(Conflict) as err:  # the note is refused after the step's row is written: the move is undone with it
        store.move_lifecycle_step_with_event(request_id=req.id, step="deploy", to_state="done", now=T0, event="completed", note=COMPLETED,
                                             merge_sha="3" * 40)
    assert err.value.code == "event_exists"
    assert store.lifecycle_steps(req.id)[0].state == "pr_open"
    assert [e.step for e in store.lifecycle_events(req.id)] == ["requested", "pr_opened", "completed"]
    done, merged = store.move_lifecycle_step_with_event(request_id=req.id, step="deploy", to_state="done", now=T0, event="merged",
                                                        note="Merged.", merge_sha="3" * 40)
    assert (done.state, done.merge_sha, merged.step) == ("done", "3" * 40, "merged")


def test_a_command_the_writer_does_not_carry_out_is_finished_with_its_note(store):
    """A refused, late or no-op command is finished with a note (the "command" step) in the same transaction: never silence."""
    _, admin = people(store)
    req, _ = writing_lifecycle(store, admin)
    made = store.add_lifecycle_command(request_id=req.id, command="retry", actor_id=admin.id, now=T0)
    store.finish_lifecycle_command(command_id=made.id, now=T0, note="Retry asked by Admin, but no step had failed: nothing changed.")
    assert store.open_lifecycle_command(req.id) is None
    assert [(e.step, e.note) for e in store.lifecycle_events(req.id)][-1] == (
        "command", "Retry asked by Admin, but no step had failed: nothing changed.")
    again = store.add_lifecycle_command(request_id=req.id, command="retry", actor_id=admin.id, now=T0)
    with pytest.raises(ValueError):
        store.finish_lifecycle_command(command_id=again.id, now=T0, note="")
    assert store.open_lifecycle_command(req.id) is not None, "no note: not finished either"
    store.finish_lifecycle_command(command_id=again.id, now=T0)  # without a note, as before
    assert [e.step for e in store.lifecycle_events(req.id)].count("command") == 1


def test_an_abandon_closes_its_steps_finishes_its_command_notes_it_and_fails_the_request_in_one_transaction(store):
    """Every step not done is closed in the same transaction as the command, the note and writing -> failed: a crash between them
    never leaves closed steps under a writing request."""
    _, admin = people(store)
    req, _ = writing_lifecycle(store, admin, action="decommission")
    store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="pr_open", now=T0, **PR)
    made = store.add_lifecycle_command(request_id=req.id, command="abandon", actor_id=admin.id, now=T0)
    other = lifecycle_request(store, admin, repo="acme/tools")
    store.claim_approved(now=T0)
    theirs = store.add_lifecycle_command(request_id=other.id, command="abandon", actor_id=admin.id, now=T0)
    with pytest.raises(Conflict):  # another request's command: nothing changes, not even a step
        store.abandon_lifecycle(request_id=req.id, command_id=theirs.id, note="Abandoned by Admin.", now=T0)
    assert [s.state for s in store.lifecycle_steps(req.id)] == ["pr_open", "planned", "planned"]
    with pytest.raises(ValueError):  # no note: nothing
        store.abandon_lifecycle(request_id=req.id, command_id=made.id, note="", now=T0)
    assert store.get_request(req.id).state == "writing" and store.open_lifecycle_command(req.id) is not None
    failed = store.abandon_lifecycle(request_id=req.id, command_id=made.id, note="Abandoned by Admin.", now=T0)
    assert (failed.state, failed.failure_reason) == ("failed", "abandoned")
    assert [s.state for s in store.lifecycle_steps(req.id)] == ["closed", "closed", "closed"]
    assert store.open_lifecycle_command(req.id) is None
    assert [(e.step, e.note) for e in store.lifecycle_events(req.id)][-1] == ("abandoned", "Abandoned by Admin.")
    with pytest.raises(Conflict):
        store.abandon_lifecycle(request_id=req.id, command_id=made.id, note="Abandoned by Admin.", now=T0)


def test_an_abandon_keeps_a_done_step_and_a_closed_one(store):
    _, admin = people(store)
    req, _ = writing_lifecycle(store, admin, action="decommission")
    store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="done", now=T0)
    store.move_lifecycle_step(request_id=req.id, step="archive", to_state="failed", now=T0, failure_reason="archive: not installed")
    store.move_lifecycle_step(request_id=req.id, step="factory", to_state="closed", now=T0)
    made = store.add_lifecycle_command(request_id=req.id, command="abandon", actor_id=admin.id, now=T0)
    store.abandon_lifecycle(request_id=req.id, command_id=made.id, note="Abandoned by Admin.", now=T0)
    assert [(s.state, s.failure_reason) for s in store.lifecycle_steps(req.id)] == [("done", None), ("closed", None), ("closed", None)], \
        "a closed step keeps no failure reason (0012: only a failed step has one)"


def test_a_stop_closes_the_steps_still_to_do_finishes_its_command_notes_it_and_fails_the_request_in_one_transaction(store):
    """I-1: a decommission whose deploy step is done ends failed "stopped": its remaining steps closed, the stop command finished and
    its "stopped" note, all at once. The state the deploy step set stays."""
    _, admin = people(store)
    req, _ = writing_lifecycle(store, admin, action="decommission")
    store.set_lifecycle_state(request_id=req.id, state="decommissioned", now=T0)
    store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="done", now=T0)
    store.move_lifecycle_step(request_id=req.id, step="archive", to_state="failed", now=T0, failure_reason="archive: github: 404")
    abandon = store.add_lifecycle_command(request_id=req.id, command="abandon", actor_id=admin.id, now=T0)
    with pytest.raises(Conflict):  # an abandon is not a stop
        store.stop_lifecycle(request_id=req.id, command_id=abandon.id, note="Stopped by Admin.", now=T0)
    store.finish_lifecycle_command(command_id=abandon.id, now=T0)
    made = store.add_lifecycle_command(request_id=req.id, command="stop", actor_id=admin.id, now=T0)
    assert made.command == "stop"
    with pytest.raises(ValueError):  # no note: nothing
        store.stop_lifecycle(request_id=req.id, command_id=made.id, note="", now=T0)
    assert store.get_request(req.id).state == "writing" and store.open_lifecycle_command(req.id) is not None
    failed = store.stop_lifecycle(request_id=req.id, command_id=made.id, note="Stopped by Admin.", now=T0)
    assert (failed.state, failed.failure_reason) == ("failed", "stopped")
    assert [s.state for s in store.lifecycle_steps(req.id)] == ["done", "closed", "closed"]
    assert store.open_lifecycle_command(req.id) is None
    assert [(e.step, e.note) for e in store.lifecycle_events(req.id)][-1] == ("stopped", "Stopped by Admin.")
    assert store.lifecycle_state("acme/notes").state == "decommissioned"
    with pytest.raises(Conflict):
        store.stop_lifecycle(request_id=req.id, command_id=made.id, note="Stopped by Admin.", now=T0)


def test_a_stop_is_only_for_a_decommission_whose_deploy_step_is_done(store):
    _, admin = people(store)
    req, _ = writing_lifecycle(store, admin, action="decommission")
    made = store.add_lifecycle_command(request_id=req.id, command="stop", actor_id=admin.id, now=T0)
    with pytest.raises(Conflict):
        store.stop_lifecycle(request_id=req.id, command_id=made.id, note="Stopped by Admin.", now=T0)
    assert [s.state for s in store.lifecycle_steps(req.id)] == ["planned", "planned", "planned"]
    suspend, _ = writing_lifecycle(store, admin, repo="acme/tools")
    store.move_lifecycle_step(request_id=suspend.id, step="deploy", to_state="done", now=T0)
    theirs = store.add_lifecycle_command(request_id=suspend.id, command="stop", actor_id=admin.id, now=T0)
    with pytest.raises(Conflict):
        store.stop_lifecycle(request_id=suspend.id, command_id=theirs.id, note="Stopped by Admin.", now=T0)
    assert store.get_request(suspend.id).state == "writing" and store.open_lifecycle_command(suspend.id) is not None


def test_a_retry_moves_its_step_notes_it_and_finishes_its_command_in_one_transaction(store):
    """I-3: never a retried step whose command is still open (the next pass would retry it again), nor a finished command whose step
    never moved."""
    _, admin = people(store)
    req, _ = writing_lifecycle(store, admin)
    store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="pr_open", now=T0, **PR)
    made = store.add_lifecycle_command(request_id=req.id, command="retry", actor_id=admin.id, now=T0)
    note = "Retried by Admin: the deploy step starts again (attempt 2)."
    with pytest.raises(Conflict) as err:  # the step hasn't failed: nothing changes
        store.retry_lifecycle(request_id=req.id, command_id=made.id, step="deploy", note=note, now=T0)
    assert err.value.code == "wrong_state"
    assert store.open_lifecycle_command(req.id) is not None and "retried" not in [e.step for e in store.lifecycle_events(req.id)]
    store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="failed", now=T0, failure_reason="check_failed: deploy-check")
    with pytest.raises(ValueError):  # no note: nothing
        store.retry_lifecycle(request_id=req.id, command_id=made.id, step="deploy", note="", now=T0)
    other, _ = writing_lifecycle(store, admin, repo="acme/tools")
    theirs = store.add_lifecycle_command(request_id=other.id, command="retry", actor_id=admin.id, now=T0)
    for wrong in (theirs.id, 0, 10**6):  # another request's command, or none
        with pytest.raises(Conflict) as err:
            store.retry_lifecycle(request_id=req.id, command_id=wrong, step="deploy", note=note, now=T0)
        assert err.value.code == "wrong_state"
    assert store.lifecycle_steps(req.id)[0].state == "failed" and store.open_lifecycle_command(req.id).id == made.id
    assert "retried" not in [e.step for e in store.lifecycle_events(req.id)]
    moved = store.retry_lifecycle(request_id=req.id, command_id=made.id, step="deploy", note=note, now=T0)
    assert (moved.state, moved.attempt, moved.pr_number, moved.failure_reason) == ("planned", 2, None, None)
    assert store.lifecycle_steps(req.id)[0] == moved and store.open_lifecycle_command(req.id) is None
    assert [(e.step, e.note) for e in store.lifecycle_events(req.id)][-1] == ("retried", note)
    with pytest.raises(Conflict):  # finished: never twice
        store.retry_lifecycle(request_id=req.id, command_id=made.id, step="deploy", note=note, now=T0)
    store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="failed", now=T0, failure_reason="check_failed: deploy-check")
    abandon = store.add_lifecycle_command(request_id=req.id, command="abandon", actor_id=admin.id, now=T0)
    with pytest.raises(Conflict) as err:  # an abandon is not a retry
        store.retry_lifecycle(request_id=req.id, command_id=abandon.id, step="deploy", note=note, now=T0)
    assert err.value.code == "wrong_state" and store.lifecycle_steps(req.id)[0].state == "failed"


def test_a_rebuild_is_a_note_of_its_own(store):
    _, admin = people(store)
    req, _ = writing_lifecycle(store, admin)
    store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="pr_open", now=T0, **PR)
    moved, event = store.move_lifecycle_step_with_event(request_id=req.id, step="deploy", to_state="planned", now=T0, event="rebuilt",
                                                        note="Pull request #7 closed: main moved, so the writer rebuilds it.")
    assert (moved.state, moved.attempt, event.step) == ("planned", 2, "rebuilt")
