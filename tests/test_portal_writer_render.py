import dataclasses as dc

import yaml
from portal_fakes import T0, approved_request, binding_request

from portal.contracts import validate
from portal.writer.allowlist import target_for
from portal.writer.render import binding_yaml, branch_name, content_for, fenced, intake_yaml, pr_body as _pr_body, title


def pr_body(request, requester, approver, platform_user=None):
    """The body the writer opens for this request: who merges comes from its target."""
    return _pr_body(request, requester, approver, target_for("acme", request), platform_user=platform_user)


def test_the_file_is_the_approved_record_with_a_header(portal):
    req = approved_request(portal.store, portal.people, T0)
    text = intake_yaml(req.final, req.id)
    assert text.startswith("# Written by the golden road portal (request " + req.id + ").")
    assert yaml.safe_load(text) == req.final and validate("intake.v2", yaml.safe_load(text)) == []


def test_the_pr_carries_the_request_the_approver_and_the_justification(portal):
    req = approved_request(portal.store, portal.people, T0)
    body = pr_body(req, portal.people["owner"], portal.people["admin"])
    for needle in (req.id, portal.people["admin"].id, "`Admin`", req.final["approved_at"], "Purpose and classification checked.", "`moderate`"):
        assert needle in body, needle
    assert title(req) == f"Register acme/notes (portal request {req.id})" and branch_name(req.id) == f"portal/request-{req.id}"


def test_the_pr_body_names_the_evidence_commit_the_floor_and_any_override(portal):
    req = approved_request(portal.store, portal.people, T0)
    body = pr_body(req, portal.people["owner"], portal.people["admin"])
    assert "- Evidence: commit `" + "e" * 40 + "`, floor tier `low`, classification `internal`" in body
    assert "Approved below the floor" not in body
    override = {"by": req.decided_by, "at": req.final["approved_at"], "reason": "``` @org/security <img src=x> ```` override"}
    body = pr_body(dc.replace(req, final={**req.final, "floor_override": override}), portal.people["owner"], portal.people["admin"])
    assert f"- Approved below the floor by `Admin` (`{req.decided_by}`):" in body
    assert "`````text\n``` @org/security <img src=x> ```` override\n`````" in body


def test_untrusted_text_cannot_break_out_of_the_pr_body(portal):
    req = approved_request(portal.store, portal.people, T0)
    req = dc.replace(req, final={**req.final, "approval_justification": "``` @org/security <img src=x> ````"})
    sneaky = dc.replace(portal.people["admin"], display_name="`@everyone` <b>")
    body = pr_body(req, portal.people["owner"], sneaky)
    assert "`````text\n``` @org/security <img src=x> ````\n`````" in body
    assert "`'@everyone' <b>`" in body  # backticks in a name become quotes, and the whole name sits in one code span


def test_fenced_uses_a_fence_longer_than_any_backtick_run():
    assert fenced("a ```` b").startswith("`````text\n")


def test_a_binding_file_is_the_binding_with_a_header(portal):
    req = binding_request(portal.store, portal.people, T0)
    text = binding_yaml(req.final, req.id)
    assert text.startswith(f"# Written by the golden road portal (request {req.id}).")
    assert "test environment" in text.split("schema:")[0]
    assert "# Change it on the app's page in the portal.\n" in text.split("schema:")[0]
    assert yaml.safe_load(text) == req.final and validate("binding.v2", yaml.safe_load(text)) == []
    assert list(yaml.safe_load(text)) == list(req.final), "the binding's own key order"
    assert content_for(req) == text
    reg = approved_request(portal.store, portal.people, T0)
    assert content_for(reg) == intake_yaml(reg.final, reg.id)


def test_a_binding_pr_names_the_person_the_directory_entries_and_the_time(portal):
    req = binding_request(portal.store, portal.people, T0)
    assert title(req) == f"Connections for acme/notes (test) (portal request {req.id})"
    prod = binding_request(portal.store, portal.people, T0, environment="prod")
    assert title(prod) == f"Connections for acme/notes (prod) (portal request {prod.id})"
    assert "`prod`" in pr_body(prod, portal.people["owner"], None) and "composed.yaml" in pr_body(prod, portal.people["owner"], None)
    body = pr_body(req, portal.people["owner"], None)
    for needle in (f"`{req.id}`", "`acme/notes`", "`Owner`", f"(`{req.requested_by}`)", req.final["linked_at"],
                   "Azure identity `sp-notes`", "Databricks connection `wh-notes`", "`mock-directory`"):
        assert needle in body, needle
    final = {k: v for k, v in req.final.items() if k not in ("azure_identity", "databricks")}
    assert "Now linked: neither (unlinked)" in pr_body(dc.replace(req, final=final), portal.people["owner"], None)
    sneaky = dc.replace(portal.people["owner"], display_name="`@everyone` <b>")
    assert "`'@everyone' <b>`" in pr_body(req, sneaky, None)


def test_a_binding_s_pr_names_its_secrets_by_reference_only(portal):
    """A secret is named by its env, id and field (what the binding records): never a value, never a credential."""
    req = binding_request(portal.store, portal.people, T0, identity=False, secrets=[(1042, "password", "NOTES_DB_PASSWORD")])
    body = pr_body(req, portal.people["owner"], None, platform_user="golden-road-eso")
    assert "Secrets from Delinea Secret Server: `NOTES_DB_PASSWORD` (secret `1042`, field `password`)" in body
    assert "Each secret checked against `mock-secret-server` as its platform account `golden-road-eso`: still shared" in body
    assert "Now linked: neither" not in body and "no Azure identity or Databricks connection" in body
    assert "<mock>" not in body and "mock-golden-road-eso" not in body and "portal's directory" not in body
    plain = binding_request(portal.store, portal.people, T0, environment="prod")
    assert "Secret Server" not in pr_body(plain, portal.people["owner"], None)


def test_a_prod_binding_s_pr_says_the_writer_never_merges_it_and_a_code_owner_reviews_it(portal):
    """P5: who merges is in the body, from the request's target (its stored draft), never from the PR."""
    prod = binding_request(portal.store, portal.people, T0, environment="prod")
    body = pr_body(prod, portal.people["owner"], None)
    last = body.split("\n\n")[-1]
    assert last.startswith("Written by the golden road portal's writer. The writer never merges a prod binding: it needs a code owner's "
                           "review of `bindings.yaml`, and a person merges it.")
    for needle in ("Don't update the branch", "If `main` moves before the merge, the request fails, the writer closes this PR",
                   "link it again on the app's page"):
        assert needle in last, needle
    assert "It merges once the required checks pass." not in body
    test = pr_body(binding_request(portal.store, portal.people, T0), portal.people["owner"], None)
    assert "It merges once the required checks pass." in test and "never merges" not in test


def test_pr_body_has_no_default_for_who_merges(portal):
    import inspect
    assert "writer_merges" not in inspect.signature(_pr_body).parameters
    assert inspect.signature(_pr_body).parameters["target"].default is inspect.Parameter.empty
