import dataclasses as dc

import pytest
import datetime as dt

from portal_fakes import T0, approved_request, binding_request, fake_directory, writer_config

from portal.store.base import RepoAdmin

from portal.writer.allowlist import allowed, target_for
from portal.writer.recheck import recheck


def test_the_allowlist_is_intake_records_and_bindings_in_the_factory_only():
    assert allowed("acme", "acme/scorecard-factory", "intake/acme/notes.yaml") == "intake.v2"
    for repo, path in (("acme/scorecard-factory", "config/scorecard.yaml"), ("acme/scorecard-factory", "intake/acme/notes.yml"),
                       ("acme/scorecard-factory", "intake/../x.yaml"), ("acme/scorecard-factory", "intake/acme/../../x.yaml"),
                       ("acme/scorecard-factory", "suppressions/acme/notes/a.json"), ("acme/platform-pipeline", "intake/acme/notes.yaml"),
                       ("other/scorecard-factory", "intake/acme/notes.yaml"), ("acme/scorecard-factory", ".github/workflows/x.yml")):
        assert allowed("acme", repo, path) is None, (repo, path)


def ev_of(portal, req):
    return portal.store.evidence_for(req.id, req.submission)


def test_a_good_request_passes(portal):
    req = approved_request(portal.store, portal.people, T0)
    assert recheck(req, writer_config(), ev_of(portal, req)) == []
    assert target_for("acme", req).path == "intake/acme/notes.yaml"


@pytest.mark.parametrize("change, code", [
    (lambda r, p: dc.replace(r, state="approved"), "not_approved"),
    (lambda r, p: dc.replace(r, decided_by=r.requested_by, final={**r.final, "approved_by": r.requested_by}), "self_approval"),
    (lambda r, p: dc.replace(r, decision_groups=("golden-road-ops",)), "approver_not_admin"),
    (lambda r, p: dc.replace(r, decision_groups=()), "approver_not_admin"),
    (lambda r, p: dc.replace(r, decided_by=None), "no_approver"),
    (lambda r, p: dc.replace(r, repo="acme/../x"), "path_not_allowlisted"),
    (lambda r, p: dc.replace(r, final={**r.final, "tier": "critical"}), "schema_invalid"),
    (lambda r, p: dc.replace(r, final=None), "schema_invalid"),
    (lambda r, p: dc.replace(r, final={**r.final, "repo": "acme/other"}), "record_mismatch"),
    (lambda r, p: dc.replace(r, final={**r.final, "approved_by": p["ops"].id}), "record_mismatch"),
    (lambda r, p: dc.replace(r, final={**r.final, "request": "r_" + "0" * 32}), "record_mismatch"),
    (lambda r, p: dc.replace(r, kind="override"), "unknown_kind"),
])
def test_every_mismatch_fails_closed(portal, change, code):
    req = change(approved_request(portal.store, portal.people, T0), portal.people)
    assert code in recheck(req, writer_config(), ev_of(portal, req))


def test_admin_comes_from_the_groups_stored_at_the_decision_with_the_current_mapping(portal):
    req = approved_request(portal.store, portal.people, T0)
    assert "approver_not_admin" in recheck(req, writer_config(role_groups={"admin": frozenset({"renamed-admins"})}), ev_of(portal, req))


def below(req, ev):
    """The stored floor is high, and the record (which carries it) says tier moderate: below it."""
    return dc.replace(req, final={**req.final, "floor_tier": "high"}), dc.replace(ev, floor_tier="high")


def override(req, **changes):
    return {"by": req.decided_by, "at": req.final["approved_at"], "reason": "The spec over-declares egress; checked it.", **changes}


def with_override(req, ev, given, *, on_request=True):
    req = dc.replace(req, final={**req.final, "floor_override": given}, floor_override=given if on_request else None)
    return req, ev


@pytest.mark.parametrize("change, code", [
    (lambda r, e: (r, None), "no_evidence"),
    (lambda r, e: (dc.replace(r, final={**r.final, "evidence_commit": "d" * 40}), e), "evidence_mismatch"),
    (lambda r, e: (r, dc.replace(e, floor_tier="high")), "evidence_mismatch"),
    (lambda r, e: (r, dc.replace(e, floor_classification="phi")), "evidence_mismatch"),
    (lambda r, e: (dc.replace(r, final={k: v for k, v in r.final.items() if k != "evidence_commit"}), e), "evidence_mismatch"),
    (lambda r, e: below(r, e), "below_floor"),
    (lambda r, e: with_override(*below(r, e), override(r, by="p_" + "e" * 32)), "override_invalid"),
    (lambda r, e: with_override(*below(r, e), override(r, by=r.requested_by)), "override_invalid"),
    (lambda r, e: with_override(*below(r, e), override(r, reason="x" * 29)), "schema_invalid"),  # the schema (30 characters) stops it first
    (lambda r, e: with_override(*below(r, e), override(r, at="2026-10-24T12:00:00Z")), "override_invalid"),
    (lambda r, e: with_override(*below(r, e), override(r), on_request=False), "override_invalid"),
    (lambda r, e: with_override(r, e, override(r)), "override_invalid"),  # an override when nothing is below the floor
])
def test_the_floor_is_checked_against_the_stored_evidence(portal, change, code):
    req = approved_request(portal.store, portal.people, T0)
    req, evidence = change(req, ev_of(portal, req))
    assert code in recheck(req, writer_config(), evidence)


def test_an_override_by_the_approver_below_the_floor_passes(portal):
    req = approved_request(portal.store, portal.people, T0)
    req, evidence = with_override(*below(req, ev_of(portal, req)), override(req))
    assert recheck(req, writer_config(), evidence) == []


# ---- bindings: no approver; checked against the directory, and the requester must still administer the repo ----

def test_bindings_are_allowlisted_only_for_the_request_s_repo(portal):
    assert allowed("acme", "acme/scorecard-factory", "bindings/acme/notes.yaml") == "binding.v1"
    for repo, path in (("acme/scorecard-factory", "bindings/acme/../x.yaml"), ("acme/scorecard-factory", "bindings/../notes.yaml"),
                       ("acme/scorecard-factory", "bindings/acme/notes.yml"), ("acme/scorecard-factory", "bindings/acme/notes/x.yaml"),
                       ("acme/other", "bindings/acme/notes.yaml"), ("other/scorecard-factory", "bindings/acme/notes.yaml")):
        assert allowed("acme", repo, path) is None, (repo, path)
    req = binding_request(portal.store, portal.people, T0)
    t = target_for("acme", req)
    assert (t.repo, t.path, t.schema) == ("acme/scorecard-factory", "bindings/acme/notes.yaml", "binding.v1")
    assert target_for("acme", dc.replace(req, repo="acme/..")) is None


def test_the_binding_target_keeps_the_repo_s_case_as_the_request_stores_it(portal):
    req = binding_request(portal.store, portal.people, T0)
    assert target_for("acme", dc.replace(req, repo="acme/Notes")).path == "bindings/acme/Notes.yaml"


def bcfg(**changes):
    return writer_config(**{"directory": fake_directory(), **changes})


def test_a_good_binding_rechecks_clean(portal):
    req = binding_request(portal.store, portal.people, T0)
    assert recheck(req, bcfg(), None, portal.store, T0) == []


def test_an_unlinked_binding_rechecks_clean(portal):
    req = binding_request(portal.store, portal.people, T0)
    final = {k: v for k, v in req.final.items() if k not in ("azure_identity", "databricks")}
    assert recheck(dc.replace(req, final=final, draft=final), bcfg(), None, portal.store, T0) == []


IDENTITY, DATA = "azure_identity", "databricks"


@pytest.mark.parametrize("change, problem", [
    (lambda f: {**f, "repo": "acme/other"}, "record_mismatch"),
    (lambda f: {**f, "repo": "acme/Notes"}, "record_mismatch"),
    (lambda f: {**f, "request": "r_" + "c" * 32}, "record_mismatch"),
    (lambda f: {**f, "linked_by": "p_" + "c" * 32}, "record_mismatch"),
    (lambda f: {**f, IDENTITY: {**f[IDENTITY], "directory_id": "sp-other"}}, "not_in_directory"),
    (lambda f: {**f, IDENTITY: {**f[IDENTITY], "directory_id": "sp-gone"}}, "not_in_directory"),
    (lambda f: {**f, IDENTITY: {**f[IDENTITY], "client_id": "22222222-2222-4222-8222-222222222222"}}, "not_in_directory"),
    (lambda f: {**f, IDENTITY: {**f[IDENTITY], "tenant_id": "22222222-2222-4222-8222-222222222222"}}, "not_in_directory"),
    (lambda f: {**f, DATA: {**f[DATA], "directory_id": "wh-other"}}, "not_in_directory"),
    (lambda f: {**f, DATA: {**f[DATA], "host": "adb-9.9.azuredatabricks.net"}}, "not_in_directory"),
    (lambda f: {**f, DATA: {**f[DATA], "http_path": "/sql/1.0/warehouses/elsewhere"}}, "not_in_directory"),
    (lambda f: {**f, DATA: {k: v for k, v in f[DATA].items() if k != "catalog"}}, "not_in_directory"),
    (lambda f: {**f, "token": "x"}, "schema_invalid"),
    (lambda f: {k: v for k, v in f.items() if k != IDENTITY}, "schema_invalid"),  # databricks only with an identity
])
def test_a_bad_binding_is_refused(portal, change, problem):
    req = binding_request(portal.store, portal.people, T0)
    final = change(req.final)
    assert problem in recheck(dc.replace(req, final=final, draft=final), bcfg(), None, portal.store, T0)


def test_a_binding_whose_final_differs_from_its_draft_is_refused(portal):
    req = binding_request(portal.store, portal.people, T0)
    assert "record_mismatch" in recheck(dc.replace(req, draft={**req.draft, "linked_at": "2026-10-23T12:01:00Z"}), bcfg(), None, portal.store, T0)


def test_a_binding_that_is_no_longer_in_the_directory_is_refused(portal):
    from portal.directory import parse
    from portal_fakes import FAKE_DIRECTORY
    req = binding_request(portal.store, portal.people, T0)
    untagged = parse({**FAKE_DIRECTORY, "principals": [{**p, "tagged_repo_name": "elsewhere"} if p["id"] == "sp-notes" else p
                                                       for p in FAKE_DIRECTORY["principals"]]})
    assert "not_in_directory" in recheck(req, bcfg(directory=untagged), None, portal.store, T0)
    ungranted = parse({**FAKE_DIRECTORY, "databricks": [{**w, "granted_to": ["sp-other"]} for w in FAKE_DIRECTORY["databricks"]]})
    assert recheck(req, bcfg(directory=ungranted), None, portal.store, T0) == ["not_in_directory"]


def test_a_binding_without_a_directory_is_refused(portal):
    req = binding_request(portal.store, portal.people, T0)
    assert "no_directory" in recheck(req, writer_config(), None, portal.store, T0)


def test_a_binding_with_a_decision_is_refused(portal):
    req = binding_request(portal.store, portal.people, T0)
    for change in (dict(decided_by="p_" + "d" * 32), dict(decided_at=T0), dict(decision_groups=("golden-road-admins",))):
        assert "unexpected_decision" in recheck(dc.replace(req, **change), bcfg(), None, portal.store, T0), change


def test_a_binding_not_in_writing_or_of_another_path_is_refused(portal):
    req = binding_request(portal.store, portal.people, T0)
    assert "not_approved" in recheck(dc.replace(req, state="approved"), bcfg(), None, portal.store, T0)
    assert "path_not_allowlisted" in recheck(dc.replace(req, repo="acme/../x"), bcfg(), None, portal.store, T0)


def test_a_binding_whose_requester_is_no_longer_a_repo_admin_is_refused(portal):
    req = binding_request(portal.store, portal.people, T0)
    portal.store.replace_repo_admins([RepoAdmin("acme/tools", "owner-gh", 1001, T0)], observed_at=T0)  # the collector no longer sees them
    assert recheck(req, bcfg(), None, portal.store, T0) == ["requester_not_admin"]


def test_a_binding_whose_requester_s_admin_rights_are_too_old_to_rely_on_is_refused(portal):
    req = binding_request(portal.store, portal.people, T0)
    later = T0 + dt.timedelta(minutes=11)
    assert recheck(req, bcfg(), None, portal.store, later) == ["requester_not_admin"]
    assert recheck(req, bcfg(repo_admins_stale_after=dt.timedelta(minutes=20)), None, portal.store, later) == []


def test_a_binding_s_repo_is_matched_to_the_collector_s_case_insensitively(portal):
    req = binding_request(portal.store, portal.people, T0)
    portal.store.replace_repo_admins([RepoAdmin("ACME/notes", "owner-gh", 1001, T0)], observed_at=T0)
    assert recheck(req, bcfg(), None, portal.store, T0) == []


def test_a_binding_rechecked_without_the_store_fails_closed(portal):
    req = binding_request(portal.store, portal.people, T0)
    assert recheck(req, bcfg(), None) == ["requester_not_admin"]


def test_a_binding_s_admin_check_counts_only_the_requester(portal):
    req = binding_request(portal.store, portal.people, T0)
    other = dc.replace(req, requested_by=portal.people["other"].id, final={**req.final, "linked_by": portal.people["other"].id})
    other = dc.replace(other, draft=other.final)
    assert recheck(other, bcfg(), None, portal.store, T0) == ["requester_not_admin"]


def test_a_registration_s_recheck_is_unchanged_by_the_store_and_the_directory(portal):
    req = approved_request(portal.store, portal.people, T0)
    portal.store.replace_repo_admins([], observed_at=T0)
    assert recheck(req, bcfg(), ev_of(portal, req), portal.store, T0) == [] == recheck(req, writer_config(), ev_of(portal, req))
