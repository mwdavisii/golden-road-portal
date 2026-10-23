import dataclasses as dc

import yaml
from portal_fakes import T0, approved_request

from portal.contracts import validate
from portal.writer.render import branch_name, fenced, intake_yaml, pr_body, title


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


def test_untrusted_text_cannot_break_out_of_the_pr_body(portal):
    req = approved_request(portal.store, portal.people, T0)
    req = dc.replace(req, final={**req.final, "approval_justification": "``` @org/security <img src=x> ````"})
    sneaky = dc.replace(portal.people["admin"], display_name="`@everyone` <b>")
    body = pr_body(req, portal.people["owner"], sneaky)
    assert "`````text\n``` @org/security <img src=x> ````\n`````" in body
    assert "`'@everyone' <b>`" in body  # backticks in a name become quotes, and the whole name sits in one code span


def test_fenced_uses_a_fence_longer_than_any_backtick_run():
    assert fenced("a ```` b").startswith("`````text\n")
