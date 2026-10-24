import dataclasses as dc

import pytest
from portal_fakes import T0, approved_request, writer_config

from portal.writer.allowlist import allowed, target_for
from portal.writer.recheck import recheck


def test_the_allowlist_is_intake_records_in_the_factory_only():
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
