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
FINAL = {"schema": "intake/v2"}  # the store doesn't check it; the web and the writer do


def people(store):
    owner, _, _ = sign_in(store, "sub-owner")
    admin, _, _ = sign_in(store, "sub-admin", roles=["admin"], groups=["golden-road-admins"])
    return owner, admin


def test_a_registration_goes_from_pending_to_merged_and_every_step_is_audited(store):
    owner, admin = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    assert (req.state, req.repo, req.requested_by, req.draft) == ("pending", "acme/notes", owner.id, DRAFT)
    approved = store.decide(request_id=req.id, actor_id=admin.id, actor_groups=["golden-road-admins"], approve=True,
                            note="Purpose checked.", final=FINAL, now=T0 + HOUR)
    assert (approved.state, approved.decided_by, approved.decision_groups, approved.final) == ("approved", admin.id, ("golden-road-admins",), FINAL)
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
    store.decide(request_id=req.id, actor_id=admin.id, actor_groups=[], approve=True, note="ok", final=FINAL, now=T0)
    store.claim_approved(now=T0)
    store.mark_failed(request_id=req.id, reason="check_failed: factory-check", now=T0)
    store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)  # a failed one no longer blocks


def test_only_a_pending_request_is_decided_and_never_by_its_requester(store):
    owner, admin = people(store)
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    for actor in (owner,):
        with pytest.raises(Conflict) as err:
            store.decide(request_id=req.id, actor_id=actor.id, actor_groups=[], approve=True, note="mine", final=FINAL, now=T0)
        assert err.value.code == "self_decision"
    store.decide(request_id=req.id, actor_id=admin.id, actor_groups=[], approve=False, note="Name what it does not do.", final=None, now=T0)
    with pytest.raises(Conflict) as err:
        store.decide(request_id=req.id, actor_id=admin.id, actor_groups=[], approve=True, note="x", final=FINAL, now=T0)
    assert err.value.code == "wrong_state"
    with pytest.raises(Conflict):
        store.decide(request_id="r_" + "0" * 32, actor_id=admin.id, actor_groups=[], approve=True, note="x", final=FINAL, now=T0)


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
