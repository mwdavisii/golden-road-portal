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


# ---- Registered in ServiceNow (spec 2026-10-31): after Recorded, once the collector has synced the merged record ----

from portal.store.base import CmdbRecord  # noqa: E402
from portal.timeline import OFF  # noqa: E402

NUMBER = "APM0001001"


def merged(portal):
    """A registration of acme/notes, merged at the clock's time: (request, its audit entries)."""
    req = approved_request(portal.store, portal.people, portal.clock())
    portal.store.mark_pr_open(request_id=req.id, branch=f"portal/{req.id}", pr_number=7, pr_url="https://github.com/acme/scorecard-factory/pull/7",
                              head_sha="2" * 40, base_sha="1" * 40, now=portal.clock())
    portal.store.mark_merged(request_id=req.id, merge_sha="3" * 40, now=portal.clock())
    return portal.store.get_request(req.id), portal.store.audit_for("request", req.id)


def test_registered_in_servicenow_follows_recorded(portal):
    req, entries = merged(portal)
    t1 = portal.clock() + dt.timedelta(minutes=5)
    rec = CmdbRecord("acme/notes", "c" * 32, NUMBER, "f" * 64, "INSERT", t1, None, None)
    steps = timeline(req, entries, cmdb=rec)
    assert [s.label for s in steps][-2:] == ["Recorded", "Registered in ServiceNow"]
    assert (steps[-1].status, steps[-1].detail, steps[-1].at) == ("done", NUMBER, t1)


def test_off_has_no_step_not_yet_synced_is_now_and_a_failure_is_failed(portal):
    req, entries = merged(portal)
    t1 = portal.clock() + dt.timedelta(minutes=5)
    assert timeline(req, entries)[-1].label == "Recorded" and timeline(req, entries, cmdb=OFF)[-1].label == "Recorded"   # ServiceNow off
    assert (timeline(req, entries, cmdb=None)[-1].status, timeline(req, entries, cmdb=None)[-1].detail) == ("now", None)  # not synced yet
    pending = CmdbRecord("acme/notes", None, None, None, None, None, "cmdb_owner_unknown", t1)
    assert (timeline(req, entries, cmdb=pending)[-1].status, timeline(req, entries, cmdb=pending)[-1].detail) == ("failed", "cmdb_owner_unknown")


def test_a_sync_from_before_the_merge_is_not_this_registration_s(portal):
    req, entries = merged(portal)
    before, after = portal.clock() - dt.timedelta(days=1), portal.clock() + dt.timedelta(minutes=5)
    old = CmdbRecord("acme/notes", "c" * 32, NUMBER, "f" * 64, "INSERT", before, None, None)
    assert timeline(req, entries, cmdb=old)[-1].status == "now"   # a change: the collector hasn't sent this one yet
    old_failure = CmdbRecord("acme/notes", None, None, None, None, None, "cmdb_unreachable", before)
    assert timeline(req, entries, cmdb=old_failure)[-1].status == "now"
    failed_since = CmdbRecord("acme/notes", "c" * 32, NUMBER, "f" * 64, "INSERT", before, "cmdb_unreachable", after)
    assert (timeline(req, entries, cmdb=failed_since)[-1].status, timeline(req, entries, cmdb=failed_since)[-1].detail) == ("failed", "cmdb_unreachable")


def test_before_the_merge_the_step_is_not_yet(portal):
    rec = CmdbRecord("acme/notes", "c" * 32, NUMBER, "f" * 64, "INSERT", portal.clock(), None, None)
    req = portal.store.get_request(portal.seeds["request"])   # pending
    steps = timeline(req, portal.store.audit_for("request", req.id), cmdb=rec)
    assert [(s.label, s.status) for s in steps][-2:] == [("Recorded", "todo"), ("Registered in ServiceNow", "todo")]
    failed = approved_request(portal.store, portal.people, portal.clock(), repo="acme/tools")
    portal.store.mark_failed(request_id=failed.id, reason="base_moved", now=portal.clock())
    steps = timeline(portal.store.get_request(failed.id), portal.store.audit_for("request", failed.id), cmdb=None)
    assert [(s.label, s.status) for s in steps][-2:] == [("Recorded", "failed"), ("Registered in ServiceNow", "todo")]


def test_the_request_page_shows_the_step_with_the_number_only_with_servicenow_on(portal):
    req, _ = merged(portal)
    portal.clock.advance(minutes=5)
    portal.store.record_cmdb_sync(repo="acme/notes", sys_id="c" * 32, number=NUMBER, fields_hash="f" * 64, operation="INSERT", now=portal.clock())
    assert "Registered in ServiceNow" not in portal.client("owner").get(f"/requests/{req.id}").text   # PORTAL_SERVICENOW unset
    from portal_fakes import make_settings
    portal.app.state.settings = make_settings(directory=portal.settings.directory, servicenow=True)
    text = portal.client("owner").get(f"/requests/{req.id}").text
    found = re.findall(r'<li class="step-(\w+)">.*?<span class="step-label">([^<]+)</span><span class="step-state">([^<]+)</span>(.*?)</li>', text, re.S)
    assert [f[:3] for f in found][-2:] == [("done", "Recorded", "Done"), ("done", "Registered in ServiceNow", "Done")]
    assert f'<span class="step-detail">{NUMBER}</span>' in found[-1][3]


def test_a_renewal_with_the_same_values_is_registered_once_the_collector_reconciles_its_new_file(portal):
    """The renewal's intake file is new (a new blob) though its values match the last good sync: the collector sends it again (NO_CHANGE),
    so the sync is after the merge and the step is done, not Now for good."""
    from portal.collectors.cmdb import collect_cmdb
    from portal.servicenow_mock import load_mock
    from portal.store.base import IntakeObserved
    owner = portal.people["owner"]
    mock = load_mock()
    mock.users = (*mock.users, {"sys_id": "a" * 32, "user_name": "owner", "email": owner.email})   # the owner is a ServiceNow user
    def observe(blob):
        portal.store.replace_intake_observed([IntakeObserved("acme/notes", blob, "intake/v2", True, owner.id, "moderate", "public",
                                                             "Stores short team notes; no email, no files.", dt.date(2027, 10, 23), None, None)],
                                             observed_at=portal.clock())
    observe("c" * 40)
    collect_cmdb(portal.store, mock, github_web="https://github.com", now=portal.clock() - dt.timedelta(days=30))   # the first registration
    req, entries = merged(portal)   # the renewal merges, same values
    observe("d" * 40)
    collect_cmdb(portal.store, mock, github_web="https://github.com", now=portal.clock() + dt.timedelta(minutes=5))
    rec = portal.store.cmdb_record("acme/notes")
    assert rec.last_operation == "NO_CHANGE" and rec.last_error is None
    assert (timeline(req, entries, cmdb=rec)[-1].status, timeline(req, entries, cmdb=rec)[-1].detail) == ("done", rec.number)


# ---- a lifecycle request (spec 2026-11-02 "Requests") ----

from portal.timeline import lifecycle_timeline  # noqa: E402

PR = dict(branch="portal/lifecycle-x-deploy-1", pr_number=1, pr_url="https://github.com/acme/golden-road-deploy/pull/1", head_sha="2" * 40,
          base_sha="1" * 40)


def life(portal, req_id):
    store = portal.store
    found = store.get_request(req_id)
    return [(s.label, s.status, s.detail) for s in lifecycle_timeline(found, store.lifecycle_steps(req_id), store.lifecycle_events(req_id))]


def planned(portal, action="suspend"):
    from portal_fakes import lifecycle_request
    from portal.store.base import ACTION_STEPS
    req = lifecycle_request(portal.store, portal.people, portal.clock(), action=action)
    portal.store.add_lifecycle_steps(request_id=req.id, steps=ACTION_STEPS[action], now=portal.clock())
    return req


def test_a_suspend_just_requested_and_planned(portal):
    from portal_fakes import lifecycle_request
    req = lifecycle_request(portal.store, portal.people, portal.clock(), claim=False)
    assert life(portal, req.id) == [("Requested", "done", "SCTASK0010001"), ("Applied in git", "todo", None)]
    portal.store.claim_approved(now=portal.clock())
    portal.store.add_lifecycle_steps(request_id=req.id, steps=("deploy",), now=portal.clock())
    assert life(portal, req.id) == [("Requested", "done", "SCTASK0010001"), ("Deploy repo pull request", "now", None),
                                    ("Applied in git", "todo", None)]


def test_a_suspend_with_its_pr_open_then_merged_and_applied(portal):
    req = planned(portal)
    portal.store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="pr_open", now=portal.clock(), **PR)
    assert life(portal, req.id)[1:] == [("Deploy repo pull request", "now", "#1"), ("Applied in git", "todo", None)]
    portal.store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="done", merge_sha="3" * 40, now=portal.clock())
    portal.store.apply_lifecycle(request_id=req.id, merge_sha="3" * 40, note="Applied in git.", now=portal.clock())
    assert [status for _, status, _ in life(portal, req.id)] == ["done", "done", "done"]
    assert life(portal, req.id)[-1] == ("Applied in git", "done", None)


def test_a_failed_step_shows_its_reason(portal):
    req = planned(portal)
    portal.store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="failed", failure_reason="check_failed: deploy-check",
                                     now=portal.clock())
    assert life(portal, req.id)[1] == ("Deploy repo pull request", "failed", "check_failed: deploy-check")


def test_an_abandoned_request_closes_its_step(portal):
    req = planned(portal)
    portal.store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="closed", now=portal.clock())
    command = portal.store.add_lifecycle_command(request_id=req.id, command="abandon", actor_id=portal.people["admin"].id, now=portal.clock())
    portal.store.abandon_lifecycle(request_id=req.id, command_id=command.id, note="Abandoned.", now=portal.clock())
    assert life(portal, req.id)[1:] == [("Deploy repo pull request", "closed", None), ("Abandoned", "closed", None)]   # closed, as its steps: an abandon is not a failure


def test_a_stopped_request_closes_its_steps_left(portal):
    req = planned(portal, "decommission")
    portal.store.set_lifecycle_state(request_id=req.id, state="decommissioned", now=portal.clock())
    portal.store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="done", now=portal.clock())
    command = portal.store.add_lifecycle_command(request_id=req.id, command="stop", actor_id=portal.people["admin"].id, now=portal.clock())
    portal.store.stop_lifecycle(request_id=req.id, command_id=command.id, note="Stopped.", now=portal.clock())
    assert life(portal, req.id)[2:] == [("Repository archived", "closed", None), ("Factory record pull request", "closed", None),
                                        ("Stopped", "closed", None)]


def test_a_request_refused_at_the_recheck(portal):
    from portal_fakes import lifecycle_request
    req = lifecycle_request(portal.store, portal.people, portal.clock())
    portal.store.mark_failed(request_id=req.id, reason="recheck: nothing_to_suspend", now=portal.clock())
    assert life(portal, req.id) == [("Requested", "done", "SCTASK0010001"), ("Refused", "failed", "recheck: nothing_to_suspend")]


def test_a_decommission_lists_its_three_steps_in_order(portal):
    req = planned(portal, "decommission")
    assert [label for label, _, _ in life(portal, req.id)] == ["Requested", "Deploy repo pull request", "Repository archived",
                                                               "Factory record pull request", "Applied in git"]
    assert [status for _, status, _ in life(portal, req.id)][1:4] == ["now", "todo", "todo"]
