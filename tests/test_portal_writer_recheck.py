import dataclasses as dc

import pytest
import datetime as dt

from portal_fakes import T0, approved_request, binding_request, fake_directory, writer_config

from portal.store.base import RepoAdmin

from portal.writer.allowlist import allowed, target_for
from portal.writer.recheck import recheck


def test_the_allowlist_is_intake_records_in_the_factory_and_bindings_in_the_deploy_repo_only():
    assert allowed("acme", "acme/scorecard-factory", "intake/acme/notes.yaml") == "intake.v2"
    assert allowed("acme", "acme/scorecard-factory", "bindings/acme/notes.yaml") is None   # bindings moved to golden-road-deploy
    assert allowed("acme", "acme/golden-road-deploy", "intake/acme/notes.yaml") is None
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
    (lambda r, e: (r, dc.replace(e, floor_classification="protected")), "evidence_mismatch"),
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

def test_bindings_are_allowlisted_only_for_the_request_s_repo_and_environment(portal):
    D = "acme/golden-road-deploy"
    assert allowed("acme", D, "apps/acme/notes/test/bindings.yaml") == "binding.v2"
    assert allowed("acme", D, "apps/acme/notes/prod/composed.yaml") == "composed"
    for repo, path in ((D, "apps/acme/notes/test/release.yaml"), (D, "apps/acme/notes/test/kustomization.yaml"),
                       (D, "apps/acme/notes/test/base/manifests.yaml"), (D, "clusters/home/flux/acme-notes-test.yaml"),
                       (D, "clusters/office/argo/acme-notes-test.yaml"), (D, "clusters/home/flux/gitrepository.yaml"),
                       (D, "apps/acme/notes/staging/bindings.yaml"), (D, "apps/acme/../test/bindings.yaml"),
                       (D, "apps/../notes/test/bindings.yaml"), (D, "apps/acme/notes/test/bindings.yml"),
                       (D, "apps/acme/notes/test/x/bindings.yaml"), (D, ".github/workflows/deploy-check.yml"), (D, "policy/x.yaml"),
                       ("acme/scorecard-factory", "apps/acme/notes/test/bindings.yaml"), ("other/golden-road-deploy", "apps/acme/notes/test/bindings.yaml"),
                       ("acme/other", "apps/acme/notes/test/bindings.yaml")):
        assert allowed("acme", repo, path) is None, (repo, path)
    req = binding_request(portal.store, portal.people, T0)
    t = target_for("acme", req)
    assert (t.repo, t.path, t.schema, t.also) == (D, "apps/acme/notes/test/bindings.yaml", "binding.v2", "apps/acme/notes/test/composed.yaml")
    prod = binding_request(portal.store, portal.people, T0, environment="prod")
    assert (target_for("acme", prod).path, target_for("acme", prod).also) == ("apps/acme/notes/prod/bindings.yaml", "apps/acme/notes/prod/composed.yaml")
    assert target_for("acme", dc.replace(req, repo="acme/..")) is None
    for env in ("staging", None, "", "Test"):
        assert target_for("acme", dc.replace(req, draft={**req.draft, "environment": env})) is None, env
    # the path comes from the request's stored repo, never from the record's text
    assert target_for("acme", dc.replace(req, draft={**req.draft, "repo": "acme/other"}, final={**req.final, "repo": "acme/other"})).path \
        == "apps/acme/notes/test/bindings.yaml"
    reg = approved_request(portal.store, portal.people, T0)
    assert (target_for("acme", reg).repo, target_for("acme", reg).also) == ("acme/scorecard-factory", None)


def test_the_binding_target_keeps_the_repo_s_case_as_the_request_stores_it(portal):
    req = binding_request(portal.store, portal.people, T0)
    assert target_for("acme", dc.replace(req, repo="acme/Notes")).path == "apps/acme/Notes/test/bindings.yaml"


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
    (lambda f: {**f, "environment": "staging"}, "schema_invalid"),
    (lambda f: {k: v for k, v in f.items() if k not in ("environment", "secrets")} | {"schema": "binding/v1"}, "schema_invalid"),
    (lambda f: {**f, "secrets": [{"name": "x"}]}, "schema_invalid"),
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



def test_a_binding_for_prod_rechecks_clean_and_one_for_no_environment_is_refused(portal):
    prod = binding_request(portal.store, portal.people, T0, environment="prod")
    assert recheck(prod, bcfg(), None, portal.store, T0) == []
    final = {k: v for k, v in prod.final.items() if k != "environment"}
    assert {"path_not_allowlisted", "schema_invalid"} <= set(recheck(dc.replace(prod, final=final, draft=final), bcfg(), None, portal.store, T0))


# ---- a binding's Delinea secrets (spec 2026-10-29 Delinea secrets, "The writer's recheck"): as the platform account, through the
# client, each secret is still shared with golden-road-eso, its field is still a non-file field of its template, and its env name is
# still allowed. The person's own visibility can't be re-checked here (the writer has no person token): the web checked it on submit.

import json  # noqa: E402

from portal.secretserver import Client, SecretServerError  # noqa: E402
from portal.secretserver_mock import MOCK, load_mock, parse as parse_mock  # noqa: E402

NOTES_DB = (1042, "password", "NOTES_DB_PASSWORD")


def mock_doc():
    return json.loads(MOCK.read_text(encoding="utf-8"))


def mock_with(change):
    doc = mock_doc()
    change(doc)
    return parse_mock(doc)


def unshare_1042(doc):
    next(s for s in doc["secrets"] if s["id"] == 1042)["shared_with"] = []


def drop_password_field(doc):
    t = next(t for t in doc["templates"] if t["id"] == 6001)
    t["fields"] = [f for f in t["fields"] if f["fieldSlugName"] != "password"]


def password_becomes_a_file(doc):
    t = next(t for t in doc["templates"] if t["id"] == 6001)
    t["fields"] = [{**f, "isFile": True} if f["fieldSlugName"] == "password" else f for f in t["fields"]]


def scfg(**changes):
    return bcfg(**{"secret_server": load_mock(), **changes})


def with_final(req, final):
    return dc.replace(req, final=final, draft=final)


def test_a_binding_with_a_secret_rechecks_clean(portal):
    req = binding_request(portal.store, portal.people, T0, secrets=[NOTES_DB])
    assert req.final["secrets"][0]["secret_id"] == 1042 and "azure_identity" in req.final
    assert recheck(req, scfg(), None, portal.store, T0) == []


def test_a_binding_with_secrets_and_no_identity_rechecks_clean(portal):
    req = binding_request(portal.store, portal.people, T0, identity=False, secrets=[NOTES_DB, (1050, "username", "SMTP_USER")])
    assert "azure_identity" not in req.final and len(req.final["secrets"]) == 2
    assert recheck(req, scfg(), None, portal.store, T0) == []


def test_a_secrets_only_binding_needs_no_directory(portal):
    """Only the Azure identity and the Databricks connection are checked against the directory."""
    req = binding_request(portal.store, portal.people, T0, identity=False, secrets=[NOTES_DB])
    assert recheck(req, writer_config(secret_server=load_mock()), None, portal.store, T0) == []
    linked = binding_request(portal.store, portal.people, T0, environment="prod", secrets=[NOTES_DB])
    assert recheck(linked, writer_config(secret_server=load_mock()), None, portal.store, T0) == ["no_directory"]


def test_a_binding_without_secrets_needs_no_secret_server(portal):
    req = binding_request(portal.store, portal.people, T0)
    assert req.final["secrets"] == [] and recheck(req, bcfg(), None, portal.store, T0) == []


def test_a_binding_with_secrets_and_no_secret_server_is_refused(portal):
    req = binding_request(portal.store, portal.people, T0, secrets=[NOTES_DB])
    assert recheck(req, bcfg(), None, portal.store, T0) == ["no_secret_server"]


def test_a_secret_checked_by_another_secret_server_is_secret_verified_elsewhere(portal):
    """The mock is the writer's, but the reference says another Secret Server checked it: its own reason, not no_secret_server.
    binding/v2 allows only mock-secret-server, so recheck stops at the schema first; the secret check holds on its own too."""
    from portal.writer.recheck import _secret_problems
    req = binding_request(portal.store, portal.people, T0, secrets=[NOTES_DB])
    final = {**req.final, "secrets": [{**s, "verified_by": "other-secret-server"} for s in req.final["secrets"]]}
    assert _secret_problems(scfg(), final) == ["secret_verified_elsewhere"]
    assert recheck(dc.replace(req, final=final), scfg(), None, portal.store, T0) == ["schema_invalid"]


@pytest.mark.parametrize("change, problem", [
    (unshare_1042, "secret_not_shared"),
    (lambda d: d["secrets"].remove(next(s for s in d["secrets"] if s["id"] == 1042)), "secret_not_shared"),  # gone from Secret Server
    (drop_password_field, "secret_field_gone"),
    (password_becomes_a_file, "secret_field_gone"),
], ids=["unshared", "deleted", "field-removed", "field-now-a-file"])
def test_a_secret_that_changed_in_secret_server_is_refused(portal, change, problem):
    req = binding_request(portal.store, portal.people, T0, secrets=[NOTES_DB])
    assert recheck(req, scfg(secret_server=mock_with(change)), None, portal.store, T0) == [problem]


def test_a_secret_whose_template_changed_is_checked_against_its_template_now(portal):
    """The field is looked up in the template the secret has now, as the platform sees it, never one the binding recorded."""
    req = binding_request(portal.store, portal.people, T0, secrets=[NOTES_DB])

    def to_api_key(doc):
        next(s for s in doc["secrets"] if s["id"] == 1042)["secretTemplateId"] = 6003
    assert recheck(req, scfg(secret_server=mock_with(to_api_key)), None, portal.store, T0) == ["secret_field_gone"]


def test_only_the_failing_secret_matters_and_each_problem_is_named_once(portal):
    req = binding_request(portal.store, portal.people, T0, secrets=[NOTES_DB, (1050, "password", "SMTP_PASSWORD")])

    def unshare_both(doc):
        for s in doc["secrets"]:
            if s["id"] in (1042, 1050):
                s["shared_with"] = []
    assert recheck(req, scfg(secret_server=mock_with(unshare_both)), None, portal.store, T0) == ["secret_not_shared"]
    assert recheck(req, scfg(secret_server=mock_with(unshare_1042)), None, portal.store, T0) == ["secret_not_shared"]


@pytest.mark.parametrize("env", ["PORT", "HOME", "NODE_ENV", "LD_PRELOAD", "PYTHONPATH", "GUNICORN_CMD_ARGS", "UVICORN_PORT", "NPM_TOKEN",
                                 "AZURE_CLIENT_ID", "DATABRICKS_HOST", "SECRETSERVER_TOKEN"])
def test_a_secret_whose_env_name_is_now_reserved_is_refused(portal, env):
    """The schema takes any env-shaped name; the reserved names and prefixes are the writer's to hold (a hand-made draft, or a
    reserved list that grew since the person submitted)."""
    req = binding_request(portal.store, portal.people, T0, secrets=[NOTES_DB])
    final = {**req.final, "secrets": [{**req.final["secrets"][0], "env": env}]}
    assert recheck(with_final(req, final), scfg(), None, portal.store, T0) == ["secret_env_reserved"]


def test_a_reserved_env_name_is_refused_even_without_a_secret_server(portal):
    req = binding_request(portal.store, portal.people, T0, secrets=[NOTES_DB])
    final = {**req.final, "secrets": [{**req.final["secrets"][0], "env": "PORT"}]}
    assert recheck(with_final(req, final), bcfg(), None, portal.store, T0) == ["secret_env_reserved", "no_secret_server"]


@pytest.mark.parametrize("method", ["token", "secret_summary", "template_fields", "_get", "_send"])
def test_secret_server_failing_is_secret_server_unavailable_and_never_its_text(portal, monkeypatch, method):
    req = binding_request(portal.store, portal.people, T0, secrets=[NOTES_DB])

    def fail(self, *a, **kw):
        raise SecretServerError("Secret Server failed (500) answering for /api/v1/secrets with Bearer mock-golden-road-eso")
    monkeypatch.setattr(Client, method, fail)
    problems = recheck(req, scfg(), None, portal.store, T0)
    assert problems == ["secret_server_unavailable"]
    assert not any("Bearer" in p or "500" in p or "golden-road-eso" in p for p in problems)


def test_the_recheck_asks_as_the_platform_account_only_and_closes_its_client(portal, monkeypatch):
    req = binding_request(portal.store, portal.people, T0, secrets=[NOTES_DB, (1050, "username", "SMTP_USER")])
    server = load_mock()
    seen, closed = [], []
    real_token, real_close = Client.token, Client.close

    def token(self, username, password):
        seen.append(username)
        return real_token(self, username, password)

    def close(self):
        closed.append(self)
        return real_close(self)
    monkeypatch.setattr(Client, "token", token)
    monkeypatch.setattr(Client, "close", close)
    assert recheck(req, scfg(secret_server=server), None, portal.store, T0) == []
    assert seen == ["golden-road-eso"], "one token, the platform's: the writer never mints a person's"
    assert len(closed) == 1


def test_the_client_is_closed_when_secret_server_fails(portal, monkeypatch):
    req = binding_request(portal.store, portal.people, T0, secrets=[NOTES_DB])
    closed = []
    real_close = Client.close
    monkeypatch.setattr(Client, "close", lambda self: (closed.append(self), real_close(self)))

    def fail(self, *a, **kw):
        raise SecretServerError("Secret Server failed (503)")
    monkeypatch.setattr(Client, "template_fields", fail)
    assert recheck(req, scfg(), None, portal.store, T0) == ["secret_server_unavailable"] and len(closed) == 1


def test_a_secret_server_that_is_not_the_mock_is_refused(portal):
    """Only the mock is wired (MOCK ONLY): the platform account's real credential isn't, so anything else fails closed."""
    import dataclasses
    req = binding_request(portal.store, portal.people, T0, secrets=[NOTES_DB])

    @dataclasses.dataclass(frozen=True)
    class Other:
        name = "real-secret-server"
        platform_user = "golden-road-eso"

        def client(self):
            raise AssertionError("never asked")
    assert recheck(req, scfg(secret_server=Other()), None, portal.store, T0) == ["no_secret_server"]


def test_a_change_that_drops_the_scorecard_mark_is_refused(portal):
    import dataclasses as dc
    from portal_fakes import approved_change, record_view
    req = approved_change(portal.store, portal.people, T0, record=record_view(portal.people["owner"].id, scorecard="central"))
    evidence = portal.store.evidence_for(req.id, req.submission)
    assert recheck(req, writer_config(), evidence) == []
    dropped = dc.replace(req, final={k: v for k, v in req.final.items() if k != "scorecard"})
    assert "scorecard_changed" in recheck(dropped, writer_config(), evidence)


# MCP servers and skills (spec 2026-10-31): each server still on the environment's gateway, ready and usable with a service token;
# the gateway the one the writer's settings name; each skill still in the catalog at its pin, approved for the environment, and
# within the caps. The person's own view (Cloudflare's per-user policy, a skill's audience) can't be rechecked: no person here.

from portal.cloudflare import CloudflareError, Client as CloudflareClient  # noqa: E402
from portal.cloudflare_mock import MOCK as CF_MOCK  # noqa: E402
from portal.skills_catalog import MOCK as CATALOG_MOCK, MockSkillsCatalog, Skill, load_mock as load_catalog  # noqa: E402
from portal_fakes import mock_cloudflare  # noqa: E402
from portal.writer.recheck import _mcp_problems, _skill_problems  # noqa: E402

MAIN = json.loads(CATALOG_MOCK.read_text(encoding="utf-8"))["main"]
OLD = next(c for c in json.loads(CATALOG_MOCK.read_text(encoding="utf-8"))["commits"] if c != MAIN)


def cf_with(change):
    doc = json.loads(CF_MOCK.read_text(encoding="utf-8"))
    change(doc)
    return mock_cloudflare(doc=doc)


def portal_of(doc, environment="test"):
    return next(p for p in doc["portals"] if p["environment"] == environment)


def notes_of(doc):
    return next(s for s in portal_of(doc)["servers"] if s["server_id"] == "notes")


def catalog_with(tmp_path, change):
    doc = json.loads(CATALOG_MOCK.read_text(encoding="utf-8"))
    change(doc)
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return load_catalog(path)


def mcfg(**changes):
    return writer_config(**{"cloudflare": mock_cloudflare(), "skills_catalog": load_catalog(), **changes})


def mcp_and_skills(portal, **kw):
    return binding_request(portal.store, portal.people, T0, identity=False, mcp=["notes"], skills=[("incident-triage", MAIN)], **kw)


def test_a_binding_with_only_mcp_servers_and_skills_rechecks_clean_without_a_directory(portal):
    req = mcp_and_skills(portal)
    assert "azure_identity" not in req.final and req.final["mcp"]["servers"][0]["server_id"] == "notes"
    assert recheck(req, mcfg(), None, portal.store, T0) == []


@pytest.mark.parametrize("environment", ["test", "prod"])
def test_a_binding_with_an_identity_mcp_servers_and_skills_rechecks_clean(portal, environment):
    req = binding_request(portal.store, portal.people, T0, environment=environment, mcp=["notes"], skills=[("incident-triage", MAIN)])
    assert recheck(req, mcfg(directory=fake_directory()), None, portal.store, T0) == []


def test_a_binding_without_mcp_or_skills_needs_neither_cloudflare_nor_the_catalog(portal):
    req = binding_request(portal.store, portal.people, T0)
    assert recheck(req, bcfg(), None, portal.store, T0) == []


def rename_test_gateway(doc):
    portal_of(doc)["hostname"] = "mcp-elsewhere.example.com"


@pytest.mark.parametrize("change, problem", [
    (lambda d: notes_of(d).update(status="error"), "mcp_server_not_ready"),
    (lambda d: portal_of(d)["servers"].remove(notes_of(d)), "mcp_server_gone"),
    (lambda d: notes_of(d).update(on_behalf=True), "mcp_server_user_auth"),
    (rename_test_gateway, "mcp_gateway_gone"),
], ids=["not-ready", "removed", "on-behalf", "gateway-renamed"])
def test_an_mcp_server_that_changed_in_cloudflare_is_refused(portal, change, problem):
    req = mcp_and_skills(portal)
    assert recheck(req, mcfg(cloudflare=cf_with(change)), None, portal.store, T0) == [problem]


def test_a_binding_with_mcp_servers_and_no_cloudflare_is_refused(portal):
    req = mcp_and_skills(portal)
    assert recheck(req, mcfg(cloudflare=None), None, portal.store, T0) == ["no_cloudflare"]


def test_a_cloudflare_that_is_not_the_mock_is_refused(portal):
    """Only the mock is wired (MOCK ONLY): a real account's token isn't, so anything else fails closed."""
    class Other:
        name = "cloudflare"

        def client(self):
            raise AssertionError("never asked")
    req = mcp_and_skills(portal)
    assert recheck(req, mcfg(cloudflare=Other()), None, portal.store, T0) == ["no_cloudflare"]


def test_an_mcp_server_checked_by_another_cloudflare_is_mcp_verified_elsewhere(portal):
    """binding/v2 allows only mock-cloudflare, so recheck stops at the schema first; the MCP check holds on its own too."""
    req = mcp_and_skills(portal)
    final = {**req.final, "mcp": {**req.final["mcp"], "servers": [{**s, "verified_by": "cloudflare"} for s in req.final["mcp"]["servers"]]}}
    assert _mcp_problems(mcfg(), final) == ["mcp_verified_elsewhere"]
    assert recheck(with_final(req, final), mcfg(), None, portal.store, T0) == ["schema_invalid"]


@pytest.mark.parametrize("servers", [True, False], ids=["with-servers", "servers-empty"])
def test_a_gateway_that_is_not_the_writer_s_for_the_environment_is_mcp_gateway_mismatch(portal, servers):
    """As deploy-verify holds mcp.gateway to platform.json's, servers [] included: a portal and a platform config that disagree fail
    here, never silently at deploy-check."""
    req = mcp_and_skills(portal)
    final = req.final if servers else {**req.final, "mcp": {"gateway": req.final["mcp"]["gateway"], "servers": []}}
    moved = mcfg(mcp_gateways={"test": "mcp-new.example.com", "prod": "mcp.example.com"})
    assert recheck(with_final(req, final), moved, None, portal.store, T0) == ["mcp_gateway_mismatch"]


def test_the_request_that_clears_a_moved_gateway_is_never_refused(portal):
    """Batch 4: a confirmed Remove off a moved gateway writes {gateway: <the new host>, servers: []}. It names no server, so Cloudflare
    isn't asked (it may not even list the new gateway yet), and its gateway is the writer's."""
    req = mcp_and_skills(portal)
    cleared = {**req.final, "mcp": {"gateway": "mcp-new.example.com", "servers": []}}
    moved = mcfg(mcp_gateways={"test": "mcp-new.example.com", "prod": "mcp.example.com"}, cloudflare=None)
    assert recheck(with_final(req, cleared), moved, None, portal.store, T0) == []


@pytest.mark.parametrize("method", ["gateway", "_page"])
def test_cloudflare_failing_is_cloudflare_unavailable_and_never_its_text(portal, monkeypatch, method):
    req = mcp_and_skills(portal)

    def fail(self, *a, **kw):
        raise CloudflareError("Cloudflare answered 500 with Bearer mock-cloudflare-token")
    monkeypatch.setattr(CloudflareClient, method, fail)
    problems = recheck(req, mcfg(), None, portal.store, T0)
    assert problems == ["cloudflare_unavailable"] and not any("Bearer" in p or "500" in p for p in problems)


def test_the_cloudflare_client_is_closed_after_the_recheck(portal, monkeypatch):
    req = mcp_and_skills(portal)
    closed = []
    real_close = CloudflareClient.close
    monkeypatch.setattr(CloudflareClient, "close", lambda self: (closed.append(self), real_close(self)))
    assert recheck(req, mcfg(), None, portal.store, T0) == [] and len(closed) == 1


@pytest.mark.parametrize("repo", ["acme_x/notes", "-acme/notes", "acme-/notes", "ac--me/notes", "a" * 40 + "/notes"])
def test_an_owner_that_is_not_a_github_owner_is_refused_since_it_has_no_token_name(repo):
    """Spec M5: the token's name is one app's only for a GitHub owner, so the writer refuses any other, servers [] included."""
    final = {"schema": "binding/v2", "repo": repo, "environment": "test", "mcp": {"gateway": "mcp-test.example.com", "servers": []}}
    assert _mcp_problems(mcfg(), final) == ["mcp_owner_invalid"]
    assert _mcp_problems(mcfg(), {**final, "repo": "Acme/Notes"}) == [], "GitHub names are case-insensitive: the name is lower case"


def test_skills_with_no_catalog_are_refused(portal):
    req = mcp_and_skills(portal)
    assert recheck(req, mcfg(skills_catalog=None), None, portal.store, T0) == ["no_skills_catalog"]


def test_a_skill_checked_by_another_catalog_is_skill_verified_elsewhere(portal):
    req = mcp_and_skills(portal)
    final = {**req.final, "skills": [{**s, "verified_by": "skills-catalog"} for s in req.final["skills"]]}
    assert _skill_problems(mcfg(), final) == ["skill_verified_elsewhere"]
    assert recheck(with_final(req, final), mcfg(), None, portal.store, T0) == ["schema_invalid"]


def test_a_skill_gone_from_the_catalog_at_its_commit_is_refused(portal, tmp_path):
    req = mcp_and_skills(portal)
    catalog = catalog_with(tmp_path, lambda d: d["commits"][MAIN]["skills"].pop("incident-triage"))
    assert recheck(req, mcfg(skills_catalog=catalog), None, portal.store, T0) == ["skill_gone"]


def test_a_skill_pinned_to_an_older_commit_is_read_at_that_commit(portal, tmp_path):
    req = binding_request(portal.store, portal.people, T0, identity=False, skills=[("incident-triage", OLD)])
    assert recheck(req, mcfg(), None, portal.store, T0) == []
    catalog = catalog_with(tmp_path, lambda d: d["commits"][OLD]["skills"].pop("incident-triage"))
    assert recheck(req, mcfg(skills_catalog=catalog), None, portal.store, T0) == ["skill_gone"]


def test_a_skill_no_longer_approved_for_the_environment_is_refused(portal, tmp_path):
    req = mcp_and_skills(portal)

    def prod_only(doc):
        files = doc["commits"][MAIN]["skills"]["incident-triage"]
        files["skill.yaml"] = files["skill.yaml"].replace("environments: [test, prod]", "environments: [prod]")
    assert recheck(req, mcfg(skills_catalog=catalog_with(tmp_path, prod_only)), None, portal.store, T0) == ["skill_environment"]


def big_catalog(**files_by_name):
    """A catalog built past load_mock's checks: a skill whose files break the caps (load_mock refuses such a catalog whole)."""
    skills = {name: Skill(name, "Too big.", ("test", "prod"), "everyone", files) for name, files in files_by_name.items()}
    return MockSkillsCatalog("example-org/golden-road-skills", MAIN, {MAIN: skills})


def test_a_skill_over_the_caps_is_skill_too_large(portal):
    req = mcp_and_skills(portal)
    over = big_catalog(**{"incident-triage": {"SKILL.md": "x" * (256 * 1024 + 1)}})
    assert recheck(req, mcfg(skills_catalog=over), None, portal.store, T0) == ["skill_too_large"]


def test_skills_over_the_environment_s_cap_together_are_skill_too_large():
    """Each skill under 256 KiB, together over 512 KiB: the one ConfigMap can't hold them."""
    third = "x" * (200 * 1024)
    catalog = big_catalog(**{name: {"SKILL.md": third} for name in ("a-skill", "b-skill", "c-skill")})
    final = {"schema": "binding/v2", "repo": "acme/notes", "environment": "test",
             "skills": [{"name": n, "commit": MAIN, "verified_by": catalog.name} for n in ("a-skill", "b-skill", "c-skill")]}
    assert _skill_problems(mcfg(skills_catalog=catalog), final) == ["skill_too_large"]
    assert _skill_problems(mcfg(skills_catalog=catalog), {**final, "skills": final["skills"][:2]}) == []


def test_an_mcp_and_a_skill_problem_are_both_named(portal, tmp_path):
    req = mcp_and_skills(portal)
    cfg = mcfg(cloudflare=cf_with(lambda d: notes_of(d).update(status="error")),
               skills_catalog=catalog_with(tmp_path, lambda d: d["commits"][MAIN]["skills"].pop("incident-triage")))
    assert recheck(req, cfg, None, portal.store, T0) == ["mcp_server_not_ready", "skill_gone"]
