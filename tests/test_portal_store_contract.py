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


DRAFT = {"schema": "registration-draft/v1", "repo": "acme/notes", "purpose": "Stores short team notes.",
         "data_classification": "internal", "proposed_tier": "low"}


def final_for(evidence, **changes):
    """The final record an approval carries: the store checks its tier, classification and the evidence it names (the rest is the web's)."""
    return {"schema": "intake/v2", "tier": "moderate", "data_classification": "internal", "approved_at": "2026-10-23T12:00:00Z", "evidence_commit": evidence.commit,
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
    stub = Evidence(req.id, 1, "e" * 40, {}, "low", "internal", "f" * 40, T0)
    with pytest.raises(Conflict, match="no_evidence"):
        approve(store, req, admin, final_for(stub))
    give_evidence(store, req)
    store.decide(request_id=req.id, actor_id=admin.id, actor_groups=["g"], approve=False, note="Say what it stores.", final=None, now=T0)
    store.resubmit(request_id=req.id, person_id=owner.id, draft=DRAFT, now=T0)
    with pytest.raises(Conflict, match="no_evidence"):  # submission 1's evidence doesn't count for submission 2
        approve(store, req, admin, final_for(stub))
    assert store.get_request(req.id).state == "pending"


@pytest.mark.parametrize("change", [{"evidence_commit": "d" * 40}, {"floor_tier": "high"}, {"floor_classification": "phi"}])
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
