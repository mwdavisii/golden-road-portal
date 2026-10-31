"""The Re-review page (spec 2026-10-31 R1, R6 to R8, R10): admins only, open items with what opened them, closed with a note."""

import dataclasses as dc
import datetime as dt

import pytest
from portal_fakes import T0

from portal.store.base import IntakeObserved

SIG = [{"id": "T-17", "kind": "trigger", "level": "elevated", "summary": "resources_tier raised from default to medium", "count": 1},
       {"id": "T-5", "kind": "trigger", "level": "elevated", "summary": "docs changed", "count": 2}]


def observe(portal, owner=None, extra=()):
    rows = [IntakeObserved("acme/notes", "c" * 40, "intake/v2", True, owner or "p_" + "9" * 32, "low", "public", "Stores notes.",
                           dt.date(2026, 10, 1), None, None), *extra]
    portal.store.replace_intake_observed(rows, observed_at=T0)


def overdue(portal, opened=T0):
    return portal.store.open_rereview_item(repo="acme/notes", reason="review_overdue", key="2026-10-01", now=opened)


def signal(portal, head="a", pr=7):
    portal.store.add_rereview_event(doc={"schema": "re-review-event/v1", "repo": "acme/notes", "pr": pr, "head_sha": head * 40,
                                         "tree_sha": "b" * 40, "scorecard_version": "0.5.1", "created": "2026-10-31T12:00:00Z",
                                         "level": "elevated", "floor": "standard", "complete": True, "signals": SIG},
                                    run_repo="acme/scorecard-factory", run_id=ord(head), now=T0)
    return portal.store.open_rereview_item(repo="acme/notes", reason="scorecard_signal", key=head * 40, now=T0, event=(head * 40, ("T-17",)))


def test_the_page_lists_open_items_with_their_reason_in_words(portal):
    observe(portal)
    overdue(portal)
    signal(portal)
    text = portal.client("admin").get("/admin/rereview").text
    assert "<h1>Re-review</h1>" in text and "2 open, 0 open more than 30 days." in text
    assert '<span class="badge">Review overdue</span>' in text and '<span class="badge">Scorecard signal</span>' in text
    assert "Review by 2026-10-01: the date has passed." in text
    assert "<td>#7</td>" in text and f'<span class="mono sha" title="{"a" * 40}">{"a" * 12}</span>' in text
    assert '<li><span class="mono">T-17</span>: resources_tier raised from default to medium</li>' in text
    assert "docs changed" not in text   # only the signals that matched are listed


def test_an_item_open_longer_than_the_stale_days_is_flagged_and_counted(portal):
    observe(portal)
    overdue(portal, opened=T0 - dt.timedelta(days=31))   # the memory store keeps the time it is given (the database uses its own)
    text = portal.client("admin").get("/admin/rereview").text
    assert "1 open, 1 open more than 30 days." in text and '<span class="tag">Open 31 days</span>' in text
    portal.app.state.settings = dc.replace(portal.settings, rereview_stale_days=0)
    text = portal.client("admin").get("/admin/rereview").text
    assert "1 open." in text and "Open 31 days" not in text


def test_closing_needs_a_note_and_is_audited(portal):
    observe(portal)
    item = overdue(portal)
    client, csrf = portal.client("admin"), portal.csrfs["admin"]
    short = client.post(f"/admin/rereview/{item.id}/close", data={"csrf_token": csrf, "note": "ok", "events_seen": "0"})
    assert short.status_code == 400 and "Write what you decided, in 10 to 1000 characters on one line." in short.text
    done = client.post(f"/admin/rereview/{item.id}/close", data={"csrf_token": csrf, "note": "Owner renewing; request r_1 is open.", "events_seen": "0"})
    assert done.status_code == 303 and done.headers["location"] == "/admin/rereview"
    assert portal.store.rereview_items() == []
    entry = portal.store.audit_for("rereview_item", item.id)[-1]
    assert (entry.action, entry.actor_id, entry.reason) == ("rereview_item.closed", portal.people["admin"].id, "Owner renewing; request r_1 is open.")
    again = client.post(f"/admin/rereview/{item.id}/close", data={"csrf_token": csrf, "note": "Closing it a second time.", "events_seen": "0"})
    assert again.status_code == 409


def test_an_admin_who_owns_the_app_can_t_close_its_item(portal):
    observe(portal, owner=portal.people["admin"].id)
    item = overdue(portal)
    text = portal.client("admin").get("/admin/rereview").text
    assert "You own this app, so another admin must close this." in text and f'action="/admin/rereview/{item.id}/close"' not in text
    refused = portal.client("admin").post(f"/admin/rereview/{item.id}/close", data={"csrf_token": portal.csrfs["admin"],
                                                                                    "note": "I own it, but closing anyway.", "events_seen": "0"})
    assert refused.status_code == 403 and portal.store.rereview_items() != []


def test_an_unknown_item_is_a_404(portal):
    response = portal.client("admin").post("/admin/rereview/i_" + "0" * 32 + "/close",
                                           data={"csrf_token": portal.csrfs["admin"], "note": "There is no such item.", "events_seen": "0"})
    assert response.status_code == 404


def test_the_sources_problems_and_counts_are_shown(portal):
    portal.store.set_rereview_status(source="events", problem="actions_unreadable", counts={}, now=T0)
    portal.store.set_rereview_status(source="intake", problem="factory_unreadable", counts={}, now=T0)
    portal.store.record_rereview_run(repo="acme/scorecard-factory", run_id=1, outcome="invalid", now=T0)
    portal.store.record_rereview_run(repo="acme/scorecard-factory", run_id=2, outcome="event", now=T0)
    text = portal.client("admin").get("/admin/rereview").text
    assert "actions_unreadable: the read App can't read Actions" in text and "factory_unreadable" in text
    assert "1 scorecard run was not read for the list" in text and '<div class="empty"><p>Nothing to re-review.</p></div>' in text


def test_records_outside_the_portal_and_records_it_can_t_read_are_listed_not_items(portal):
    observe(portal, extra=[IntakeObserved("acme/old", "d" * 40, "intake/v1", False, "platform-team", "high", "confidential", "A notes API.",
                                          None, None, None),
                           IntakeObserved("acme/broken", "e" * 40, None, False, None, None, None, None, None, None, "invalid")])
    text = portal.client("admin").get("/admin/rereview").text
    assert "<h2>Registered outside the portal</h2>" in text and '<li class="mono">acme/old</li>' in text
    assert "<h2>Records the portal can't read</h2>" in text and '<li><span class="mono">acme/broken</span>: is not a valid intake record' in text


def test_the_admin_s_sidebar_counts_the_open_items(portal):
    overdue(portal)
    assert '<span class="nav-count">1<span class="visually-hidden"> open</span></span>' in portal.client("admin").get("/me").text
    assert "/admin/rereview" not in portal.client("owner").get("/me").text


# ---- what the admin saw: the events, and why a record isn't an item ----

def test_the_close_form_carries_the_events_the_admin_saw_and_a_later_one_sends_them_back(portal):
    observe(portal)
    item = signal(portal)
    text = portal.client("admin").get("/admin/rereview").text
    assert f'<input type="hidden" name="events_seen" value="1">' in text
    portal.store.add_rereview_event(doc={"schema": "re-review-event/v1", "repo": "acme/notes", "pr": 8, "head_sha": "d" * 40, "tree_sha": "b" * 40,
                                         "scorecard_version": "0.5.1", "created": "2026-10-31T12:00:00Z", "level": "elevated", "floor": "standard",
                                         "complete": True, "signals": SIG}, run_repo="acme/scorecard-factory", run_id=99, now=T0)
    portal.store.add_rereview_item_event(item_id=item.id, head_sha="d" * 40, matched=("T-17",), now=T0)
    stale = portal.client("admin").post(f"/admin/rereview/{item.id}/close", data={"csrf_token": portal.csrfs["admin"],
                                                                                 "note": "Reviewed the T-17 change.", "events_seen": "1"})
    assert stale.status_code == 409 and "A scorecard event joined this item after you opened the page" in stale.text
    assert portal.store.rereview_items() != [] and '<input type="hidden" name="events_seen" value="2">' in stale.text


@pytest.mark.parametrize("seen", [None, "", "x", "-1"])
def test_a_close_without_a_count_of_what_was_seen_is_refused(portal, seen):
    observe(portal)
    item = overdue(portal)
    data = {"csrf_token": portal.csrfs["admin"], "note": "Owner renewing; request r_1 is open.", **({"events_seen": seen} if seen is not None else {})}
    assert portal.client("admin").post(f"/admin/rereview/{item.id}/close", data=data).status_code == 400
    assert portal.store.rereview_items() != []


def test_each_record_problem_and_the_actions_notice_say_what_to_do(portal):
    rows = [IntakeObserved(f"acme/{p}", "e" * 40, None, False, None, None, None, None, None, None, p)
            for p in ("invalid", "unreadable", "not_a_file", "case_mismatch")]
    observe(portal, extra=rows)
    portal.store.set_rereview_status(source="events", problem="actions_unreadable", counts={}, now=T0)
    text = portal.client("admin").get("/admin/rereview").text
    assert "Give it Actions: read" in text
    assert "is in the factory under two spellings of its name" in text and "is not a valid intake record" in text
    assert "is too large or can&#39;t be decoded" in text and "is a symlink or a submodule" in text


def test_an_event_s_summary_is_shown_as_text(portal):
    """an event comes from a run's artifact; even a trusted run's summary is untrusted text on the page."""
    observe(portal)
    hostile = [{"id": "T-17", "kind": "trigger", "level": "elevated", "summary": "<script>alert(1)</script> <b>x</b>", "count": 1}]
    portal.store.add_rereview_event(doc={"schema": "re-review-event/v1", "repo": "acme/notes", "pr": 7, "head_sha": "f" * 40, "tree_sha": "b" * 40,
                                         "scorecard_version": "0.5.1", "created": "2026-10-31T12:00:00Z", "level": "elevated", "floor": "standard",
                                         "complete": True, "signals": hostile}, run_repo="acme/scorecard-factory", run_id=77, now=T0)
    portal.store.open_rereview_item(repo="acme/notes", reason="scorecard_signal", key="f" * 40, now=T0, event=("f" * 40, ("T-17",)))
    text = portal.client("admin").get("/admin/rereview").text
    assert "<script>alert(1)</script>" not in text and "&lt;script&gt;alert(1)&lt;/script&gt; &lt;b&gt;x&lt;/b&gt;" in text


def test_an_item_names_its_owner_by_name_and_a_v1_record_s_owner_as_recorded(portal):
    observe(portal, owner=portal.people["owner"].id, extra=[IntakeObserved("acme/old", "d" * 40, "intake/v1", False, "platform-team", "high",
                                                                          "confidential", "A notes API.", None, None, None)])
    overdue(portal)
    portal.store.open_rereview_item(repo="acme/old", reason="review_overdue", key="2026-09-01", now=T0)
    text = portal.client("admin").get("/admin/rereview").text
    assert "; owner Owner." in text and portal.people["owner"].id not in text and "; owner platform-team." in text


# ---- more cases ----

def test_each_collector_part_shows_when_it_last_worked_and_a_stopped_one_is_flagged(portal):
    portal.store.set_rereview_status(source="repo_admins", problem=None, counts={"interval_seconds": 300}, now=T0 - dt.timedelta(hours=1))
    portal.store.set_rereview_status(source="intake", problem=None, counts={"records": 1, "unreadable": 0}, now=T0)
    text = portal.client("admin").get("/admin/rereview").text
    assert "<h2>The collector</h2>" in text and "repo admins" in text
    assert "has not worked for more than three of the collector's intervals" in text   # an hour > 3 x 300 s
    assert text.count("has not worked for more than") == 1                              # intake ran just now


def test_the_run_sources_show_how_far_down_they_are_read_and_what_waits(portal):
    portal.store.set_rereview_watermark(repo="acme/scorecard-factory", run_id=40, now=T0 - dt.timedelta(hours=2))
    portal.store.record_failed_run(repo="acme/scorecard-factory", run_id=7, attempt=1, last_attempt_at=T0, retention_days=90, now=T0)
    portal.store.hold_pending_event(source="central", run_repo="acme/scorecard-factory", run_id=8, now=T0, doc={
        "schema": "re-review-event/v1", "repo": "acme/later", "pr": 1, "head_sha": "a" * 40, "tree_sha": "b" * 40, "scorecard_version": "0.5.1",
        "created": "2026-10-31T12:00:00Z", "level": "elevated", "floor": "standard", "complete": True, "signals": []})
    text = portal.client("admin").get("/admin/rereview").text
    assert "<span class=\"mono\">acme/scorecard-factory</span>: read down to run 40, 2 hours ago; 1 run re-checked until it succeeds" in text
    assert "1 event waits for its app's intake record" in text


@pytest.mark.parametrize("problem, words", [("factory_not_main", "default branch is not main"),
                                            ("factory_unreadable", "can't read the factory, so central scorecard runs")])
def test_a_central_source_problem_is_said(portal, problem, words):
    portal.store.set_rereview_status(source="events", problem=problem, counts={}, now=T0)
    assert words in portal.client("admin").get("/admin/rereview").text


def test_the_skipped_runs_are_described_as_they_are(portal):
    portal.store.record_rereview_run(repo="acme/scorecard-factory", run_id=1, outcome="untrusted_run", now=T0)
    text = portal.client("admin").get("/admin/rereview").text
    assert "1 scorecard run was not read for the list" in text and "not trusted" in text and "not for a registered app" not in text


def test_a_count_of_events_seen_longer_than_six_digits_is_refused(portal):
    observe(portal)
    item = overdue(portal)
    response = portal.client("admin").post(f"/admin/rereview/{item.id}/close", data={"csrf_token": portal.csrfs["admin"],
                                                                                    "note": "Owner renewing; request r_1.", "events_seen": "1" * 7})
    assert response.status_code == 400


def test_an_item_whose_record_can_t_be_read_is_not_closed_until_its_owner_can_be_told(portal):
    portal.store.replace_intake_observed([IntakeObserved("acme/notes", "e" * 40, None, False, None, None, None, None, None, None, "invalid")],
                                         observed_at=T0)
    item = overdue(portal)
    response = portal.client("admin").post(f"/admin/rereview/{item.id}/close", data={"csrf_token": portal.csrfs["admin"],
                                                                                    "note": "Closing it anyway, please.", "events_seen": "0"})
    assert response.status_code == 409 and "can&#39;t be read" in response.text and portal.store.rereview_items() != []


def test_the_admin_who_last_registered_the_app_can_t_close_its_item(portal):
    """M2: the observed owner may be stale; the last approved registration's requester is the owner too."""
    from portal_fakes import approved_request
    approved_request(portal.store, {"owner": portal.people["admin"], "admin": portal.people["ops"]}, T0, repo="acme/tools", claim=False)
    observe(portal, extra=[IntakeObserved("acme/tools", "d" * 40, "intake/v2", True, "p_" + "9" * 32, "low", "public", "Builds tools.",
                                          dt.date(2026, 10, 1), None, None)])   # owned by someone else, as the factory last said
    item = portal.store.open_rereview_item(repo="acme/tools", reason="review_overdue", key="2026-10-01", now=T0)
    response = portal.client("admin").post(f"/admin/rereview/{item.id}/close", data={"csrf_token": portal.csrfs["admin"],
                                                                                    "note": "Closing my own app's item.", "events_seen": "0"})
    assert response.status_code == 403 and portal.store.rereview_items() != []


def test_a_run_given_up_on_after_twelve_passes_is_said(portal):
    portal.store.set_rereview_status(source="events", problem=None, counts={"unreadable_after_retries": 1}, now=T0)
    assert "1 scorecard run was recorded unreadable: GitHub didn&#39;t answer for it on twelve passes" in portal.client("admin").get(
        "/admin/rereview").text.replace("didn't", "didn&#39;t")
