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


def test_a_good_request_passes(portal):
    req = approved_request(portal.store, portal.people, T0)
    assert recheck(req, writer_config()) == []
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
    assert code in recheck(req, writer_config())


def test_admin_comes_from_the_groups_stored_at_the_decision_with_the_current_mapping(portal):
    req = approved_request(portal.store, portal.people, T0)
    assert "approver_not_admin" in recheck(req, writer_config(role_groups={"admin": frozenset({"renamed-admins"})}))
