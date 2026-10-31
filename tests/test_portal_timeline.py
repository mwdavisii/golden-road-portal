"""The request page's timeline (spec 2026-10-30 R5): Submitted, Sent back (when it was), Reviewed, Recorded or Failed, from the
request's own state and audit times. No new data."""

import datetime as dt
import re

from portal_fakes import approved_request

from portal.timeline import timeline


def steps(portal, req_id):
    store = portal.store
    return [(s.label, s.status) for s in timeline(store.get_request(req_id), store.audit_for("request", req_id))]


def send_back(portal, req_id):
    portal.store.decide(request_id=req_id, actor_id=portal.people["admin"].id, actor_groups=["golden-road-admins"], approve=False,
                        note="Say what it does not do.", final=None, now=portal.clock())


def test_pending(portal):
    assert steps(portal, portal.seeds["request"]) == [("Submitted", "done"), ("Reviewed", "now"), ("Recorded", "todo")]


def test_sent_back_then_resubmitted(portal):
    rid = portal.seeds["request"]
    send_back(portal, rid)
    assert steps(portal, rid) == [("Submitted", "done"), ("Sent back", "now"), ("Reviewed", "todo"), ("Recorded", "todo")]
    req = portal.store.get_request(rid)
    portal.store.resubmit(request_id=rid, person_id=portal.people["owner"].id, draft=req.draft, now=portal.clock())
    assert steps(portal, rid) == [("Submitted", "done"), ("Sent back", "done"), ("Reviewed", "now"), ("Recorded", "todo")]


def test_approved_and_being_recorded_then_merged(portal):
    req = approved_request(portal.store, portal.people, portal.clock())   # claimed by the writer: writing
    assert steps(portal, req.id) == [("Submitted", "done"), ("Reviewed", "done"), ("Recorded", "now")]
    now = portal.clock()
    portal.store.mark_pr_open(request_id=req.id, branch=f"portal/{req.id}", pr_number=7, pr_url="https://github.com/acme/scorecard-factory/pull/7",
                              head_sha="2" * 40, base_sha="1" * 40, now=now)
    portal.store.mark_merged(request_id=req.id, merge_sha="3" * 40, now=now)
    assert steps(portal, req.id) == [("Submitted", "done"), ("Reviewed", "done"), ("Recorded", "done")]


def test_failed(portal):
    req = approved_request(portal.store, portal.people, portal.clock())
    portal.store.mark_failed(request_id=req.id, reason="base_moved", now=portal.clock())
    assert steps(portal, req.id) == [("Submitted", "done"), ("Reviewed", "done"), ("Recorded", "failed")]   # shows "Failed" once


def test_approved_before_the_writer_claims_it_is_being_recorded(portal):
    req = approved_request(portal.store, portal.people, portal.clock(), claim=False)
    assert req.state == "approved" and steps(portal, req.id) == [("Submitted", "done"), ("Reviewed", "done"), ("Recorded", "now")]


def test_with_its_pull_request_open_it_is_still_being_recorded_and_it_can_fail_from_there(portal):
    req = approved_request(portal.store, portal.people, portal.clock())
    portal.store.mark_pr_open(request_id=req.id, branch=f"portal/{req.id}", pr_number=7, pr_url="https://github.com/acme/scorecard-factory/pull/7",
                              head_sha="2" * 40, base_sha="1" * 40, now=portal.clock())
    assert steps(portal, req.id) == [("Submitted", "done"), ("Reviewed", "done"), ("Recorded", "now")]
    failed_at = portal.clock() + dt.timedelta(hours=5)   # not the clock: the owner's session stays fresh
    portal.store.mark_failed(request_id=req.id, reason="base_moved", now=failed_at)
    assert steps(portal, req.id) == [("Submitted", "done"), ("Reviewed", "done"), ("Recorded", "failed")]
    text = portal.client("owner").get(f"/requests/{req.id}").text
    failed = text[text.index('<li class="step-failed">'):].split("</li>")[0]
    assert failed.count("Failed") == 1 and f'<time datetime="{failed_at.strftime("%Y-%m-%dT%H:%M:%SZ")}">' in failed


def test_the_timeline_shows_no_note_and_no_person(portal):
    rid = portal.seeds["request"]
    send_back(portal, rid)
    text = portal.client("owner").get(f"/requests/{rid}").text
    markup = text[text.index('<ol class="timeline"'):text.index("</ol>", text.index('<ol class="timeline"'))]
    for person in ("owner", "admin"):
        assert portal.people[person].display_name not in markup and (portal.people[person].email or "@") not in markup
    assert "Say what it does not do." not in markup and "Say what it does not do." in text   # the note is in its own card


def test_the_page_shows_each_step_with_a_word_and_a_time(portal):
    rid = portal.seeds["request"]
    send_back(portal, rid)
    text = portal.client("owner").get(f"/requests/{rid}").text
    found = re.findall(r'<li class="step-(\w+)">.*?<span class="step-label">([^<]+)</span><span class="step-state">([^<]+)</span>', text, re.S)
    assert found == [("done", "Submitted", "Done"), ("now", "Sent back", "Now"), ("todo", "Reviewed", "Not yet"), ("todo", "Recorded", "Not yet")]
    assert text.count("<time datetime=") == 2 and '<ol class="timeline" aria-label="Progress">' in text


def test_a_step_in_progress_has_a_neutral_mark(portal):
    text = portal.client("owner").get(f"/requests/{portal.seeds['request']}").text
    now = text[text.index('<li class="step-now">'):].split("</li>")[0]
    assert '<span class="step-dot" aria-hidden="true">&#9679;</span>' in now and "!" not in now
