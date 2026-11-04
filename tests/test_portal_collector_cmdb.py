"""The collector's CMDB part (spec 2026-10-31 N1-N4): each readable intake record that is new, changed since its last good sync, or whose
last attempt failed is sent to ServiceNow as a business application; unchanged ones are not; an unknown owner is never sent; ServiceNow
down stops the pass, shows the problem, and the next pass catches up."""

import datetime as dt
import json

import httpx
import pytest
from portal_fakes import T0, lifecycle_request

from portal.collectors.cmdb import SHORT_MAX, collect_cmdb, fields_hash, seed_mock
from portal.servicenow import UNEXPECTED, Client
from portal.servicenow_mock import MOCK, PASSWORD, parse
from portal.store.base import IntakeObserved
from portal.store.memory import MemoryStore

WEB = "https://github.com"
OWNER_EMAIL = "owner@example.test"


def mock_with(*emails):
    doc = json.loads(MOCK.read_text())
    for i, email in enumerate(emails):
        doc["users"].append({"sys_id": f"{i + 1:032x}", "user_name": f"user{i + 1}", "email": email})
    return parse(doc)


class Spy:
    """The mock behind a transport that records each request and can answer some of them itself."""

    def __init__(self, mock):
        self.mock, self.seen, self.answer = mock, [], None

    def client(self):
        return Client(self.mock.instance, http=httpx.Client(transport=httpx.MockTransport(self._handle)),
                      auth=(self.mock.integration_user, PASSWORD))

    def _handle(self, request):
        body = json.loads(request.content) if request.content else None
        self.seen.append((request.method, request.url.path, body))
        if self.answer is not None:
            got = self.answer(request, body)
            if got is not None:
                return got
        return self.mock._handle(request)

    def reconciled(self):
        return [b["items"][0]["values"] for m, p, b in self.seen if p == "/api/now/identifyreconcile"]


def row(repo="acme/notes", schema="intake/v2", owner=None, **changes):
    base = dict(repo=repo, blob_sha="c" * 40, schema=schema, by_portal=schema == "intake/v2", owner=owner, tier="low",
                data_classification="public", purpose="Stores team notes.", review_by=dt.date(2027, 10, 31) if schema == "intake/v2" else None,
                scorecard=None, problem=None)
    return IntakeObserved(**{**base, **changes})


def person(store, subject, email):
    p, _, _ = store.sign_in(issuer="https://idp.example.test", subject=subject, display_name=subject, email=email, groups=[], roles=[],
                            now=T0, max_age=dt.timedelta(hours=8))
    return p


@pytest.fixture
def store():
    return MemoryStore()


def run(store, spy, now=T0):
    return collect_cmdb(store, spy, github_web=WEB, now=now)


def test_a_first_pass_sends_each_record_once_and_a_second_sends_nothing_and_a_change_is_one_update(store):
    owner = person(store, "o", OWNER_EMAIL)
    store.replace_intake_observed([row(owner=owner.id), row("acme/tools", owner=owner.id)], observed_at=T0)
    spy = Spy(mock_with(OWNER_EMAIL))
    first = run(store, spy)
    assert (first["synced"], first["unchanged"], first["problem"]) == (2, 0, None)
    assert [(r.repo, r.number, r.last_operation, r.last_error) for r in store.cmdb_records()] == [
        ("acme/notes", "APM0001001", "INSERT", None), ("acme/tools", "APM0001002", "INSERT", None)]
    values = spy.reconciled()[0]
    assert values == {"name": "acme/notes", "short_description": "Stores team notes.", "u_tier": "low", "u_data_classification": "public",
                      "u_repo_url": "https://github.com/acme/notes", "correlation_id": "github:acme/notes", "discovery_source": "golden_road",
                      "u_review_by": "2027-10-31", "owned_by": f"{1:032x}", "operational_status": "1", "install_status": "1",
                      "u_golden_road_state": "active", "u_golden_road_task": ""}   # 0.7.0: an app never acted on is active
    second = run(store, spy)
    assert (second["synced"], second["unchanged"]) == (0, 2) and len(spy.reconciled()) == 2   # only the owner's user is looked up
    store.replace_intake_observed([row(owner=owner.id, tier="moderate"), row("acme/tools", owner=owner.id)], observed_at=T0)
    third = run(store, spy)
    assert (third["synced"], third["unchanged"]) == (1, 1) and spy.reconciled()[-1]["u_tier"] == "moderate"
    notes = store.cmdb_record("acme/notes")
    assert (notes.number, notes.last_operation) == ("APM0001001", "UPDATE")
    assert store.rereview_status()["cmdb"].problem is None and store.rereview_status()["cmdb"].counts["synced"] == 1


def test_an_intake_v1_record_is_sent_without_an_owner(store):
    store.replace_intake_observed([row(schema="intake/v1", owner="platform-team")], observed_at=T0)
    spy = Spy(mock_with())
    assert run(store, spy)["synced"] == 1
    [values] = spy.reconciled()
    assert "owned_by" not in values and "u_review_by" not in values
    assert not any(p.endswith("/sys_user") for _, p, _ in spy.seen)


def test_an_owner_without_an_email_or_with_one_no_user_has_is_not_sent_and_is_retried_until_it_matches(store):
    nobody = person(store, "n", None)
    stranger = person(store, "s", "stranger@example.test")
    store.replace_intake_observed([row(owner=nobody.id), row("acme/tools", owner=stranger.id)], observed_at=T0)
    spy = Spy(mock_with())
    got = run(store, spy)
    assert (got["owner_unknown"], got["synced"], got["problem"]) == (2, 0, "cmdb_owner_unknown") and spy.reconciled() == []
    assert [r.last_error for r in store.cmdb_records()] == ["cmdb_owner_unknown", "cmdb_owner_unknown"]
    assert store.cmdb_record("acme/notes").sys_id is None and store.rereview_status()["cmdb"].problem == "cmdb_owner_unknown"
    assert run(store, spy)["owner_unknown"] == 2   # retried every pass
    person(store, "n", "stranger@example.test")      # the person signs in with an email ServiceNow knows ...
    spy.mock = mock_with("stranger@example.test")    # ... and ServiceNow gets the other user
    again = run(store, spy)
    assert (again["synced"], again["owner_unknown"], again["problem"]) == (2, 0, None)
    assert [r.last_error for r in store.cmdb_records()] == [None, None]


def test_an_email_that_is_not_one_address_is_an_unknown_owner_never_a_query(store):
    odd = person(store, "x", "a@example.test^ORemail=b@example.test")
    store.replace_intake_observed([row(owner=odd.id)], observed_at=T0)
    spy = Spy(mock_with())
    assert run(store, spy)["owner_unknown"] == 1 and spy.seen == []


def test_an_invalid_or_unreadable_record_is_never_sent(store):
    bad = dict(schema=None, owner=None, tier=None, data_classification=None, purpose=None, review_by=None, by_portal=False)
    store.replace_intake_observed([row(problem="invalid", **bad), row("acme/tools", problem="unreadable", **bad)], observed_at=T0)
    spy = Spy(mock_with())
    got = run(store, spy)
    assert got["synced"] == 0 and spy.seen == [] and store.cmdb_records() == [] and store.rereview_status()["cmdb"].problem is None


def test_service_now_down_stops_the_pass_shows_the_problem_and_the_next_pass_catches_up(store):
    owner = person(store, "o", OWNER_EMAIL)
    store.replace_intake_observed([row("acme/a", owner=owner.id), row("acme/b", owner=owner.id), row("acme/c", owner=owner.id)],
                                  observed_at=T0)
    spy = Spy(mock_with(OWNER_EMAIL))
    calls = {"n": 0}

    def flaky(request, body):
        if request.url.path == "/api/now/identifyreconcile":
            calls["n"] += 1
            if calls["n"] == 2:
                return httpx.Response(503, json={"error": {"message": "Instance hibernating: golden-road-cmdb"}, "status": "failure"})
        return None
    spy.answer = flaky
    got = run(store, spy)
    assert (got["synced"], got["unreachable"], got["problem"]) == (1, 1, "cmdb_unreachable")
    assert [(r.repo, r.last_error) for r in store.cmdb_records()] == [("acme/a", None), ("acme/b", "cmdb_unreachable")]
    assert store.rereview_status()["cmdb"].problem == "cmdb_unreachable"
    spy.answer = None
    again = run(store, spy)
    assert (again["synced"], again["unchanged"], again["problem"]) == (2, 1, None)
    assert [(r.repo, r.number, r.last_error) for r in store.cmdb_records()] == [
        ("acme/a", "APM0001001", None), ("acme/b", "APM0001002", None), ("acme/c", "APM0001003", None)]


def test_service_now_down_after_a_good_sync_keeps_the_ci(store):
    owner = person(store, "o", OWNER_EMAIL)
    store.replace_intake_observed([row(owner=owner.id)], observed_at=T0)
    spy = Spy(mock_with(OWNER_EMAIL))
    run(store, spy)
    store.replace_intake_observed([row(owner=owner.id, tier="high")], observed_at=T0)
    spy.answer = lambda request, body: httpx.Response(500, json={})
    assert run(store, spy)["problem"] == "cmdb_unreachable"
    kept = store.cmdb_record("acme/notes")
    assert (kept.number, kept.last_error) == ("APM0001001", "cmdb_unreachable")


def test_an_item_with_errors_is_refused_and_the_rest_still_sync(store):
    owner = person(store, "o", OWNER_EMAIL)
    store.replace_intake_observed([row("acme/a", owner=owner.id), row("acme/b", owner=owner.id)], observed_at=T0)
    spy = Spy(mock_with(OWNER_EMAIL))

    def refuse_a(request, body):
        if body and body["items"][0]["values"]["name"] == "acme/a":
            return httpx.Response(200, json={"result": {"items": [{"className": "cmdb_ci_business_app", "errors": [
                {"error": "MISSING_MATCHING_ATTRIBUTES", "message": "x"}]}]}})
        return None
    spy.answer = refuse_a
    got = run(store, spy)
    assert (got["refused"], got["synced"], got["problem"]) == (1, 1, "cmdb_refused")
    assert [(r.repo, r.last_error) for r in store.cmdb_records()] == [("acme/a", "cmdb_refused"), ("acme/b", None)]


def test_an_unknown_owner_outranks_a_refusal_in_the_problem(store):
    owner, nobody = person(store, "o", OWNER_EMAIL), person(store, "n", None)
    store.replace_intake_observed([row("acme/a", owner=owner.id), row("acme/b", owner=nobody.id)], observed_at=T0)
    spy = Spy(mock_with(OWNER_EMAIL))
    spy.answer = lambda request, body: httpx.Response(200, json={"result": {"items": [{"className": "cmdb_ci_business_app",
                                                                                         "errors": [{"error": "X", "message": "x"}]}]}}) \
        if body else None
    assert run(store, spy)["problem"] == "cmdb_owner_unknown"


def test_the_purpose_is_cut_at_1000_characters(store):
    store.replace_intake_observed([row(schema="intake/v1", owner="team", purpose="é" * 4000)], observed_at=T0)
    spy = Spy(mock_with())
    run(store, spy)
    short = spy.reconciled()[0]["short_description"]
    assert len(short) == SHORT_MAX == 1000 and short.endswith("...") and short[:-3] == "é" * 997


def test_a_purpose_of_exactly_1000_characters_is_kept_whole(store):
    store.replace_intake_observed([row(schema="intake/v1", owner="team", purpose="x" * 1000)], observed_at=T0)
    spy = Spy(mock_with())
    run(store, spy)
    assert spy.reconciled()[0]["short_description"] == "x" * 1000


def test_after_a_restart_a_mock_seeded_from_the_store_keeps_the_numbers(store):
    owner = person(store, "o", OWNER_EMAIL)
    store.replace_intake_observed([row("acme/a", owner=owner.id), row("acme/b", owner=owner.id)], observed_at=T0)
    run(store, Spy(mock_with(OWNER_EMAIL)))
    restarted = mock_with(OWNER_EMAIL)
    seed_mock(restarted, store)
    spy = Spy(restarted)
    store.replace_intake_observed([row("acme/a", owner=owner.id, tier="high"), row("acme/b", owner=owner.id), row("acme/c", owner=owner.id)],
                                  observed_at=T0)
    got = run(store, spy)
    assert (got["synced"], got["unchanged"]) == (2, 1)
    assert [(r.repo, r.number, r.last_operation) for r in store.cmdb_records()] == [
        ("acme/a", "APM0001001", "UPDATE"), ("acme/b", "APM0001002", "INSERT"), ("acme/c", "APM0001003", "INSERT")]
    a = store.cmdb_record("acme/a")
    store.record_cmdb_error(repo="acme/a", error="cmdb_unreachable", now=T0)   # a failed attempt is sent again, the values unchanged
    assert run(store, spy)["synced"] == 1 and store.cmdb_record("acme/a").last_operation == "NO_CHANGE"
    assert store.cmdb_record("acme/a").sys_id == a.sys_id


def test_the_hash_follows_the_synced_fields_the_owner_s_email_and_its_user():
    base, u = row(owner="p_" + "1" * 32), "a" * 32
    h = fields_hash(base, "Owner@Example.test", u, WEB)
    assert h == fields_hash(base, "owner@example.test", u, WEB)
    for other in (fields_hash(row(owner="p_" + "1" * 32, blob_sha="d" * 40), "owner@example.test", u, WEB),   # a new intake file
                  fields_hash(row(owner="p_" + "1" * 32, tier="high"), "owner@example.test", u, WEB),
                  fields_hash(row(owner="p_" + "1" * 32, purpose="Other."), "owner@example.test", u, WEB),
                  fields_hash(row(owner="p_" + "1" * 32, review_by=dt.date(2028, 1, 1)), "owner@example.test", u, WEB),
                  fields_hash(base, "new@example.test", u, WEB), fields_hash(base, None, u, WEB),
                  fields_hash(base, "owner@example.test", "b" * 32, WEB), fields_hash(base, "owner@example.test", None, WEB),
                  fields_hash(base, "owner@example.test", u, "https://github.example")):
        assert other != h


def test_another_user_taking_over_the_owner_s_email_re_syncs_owned_by(store):
    owner = person(store, "o", OWNER_EMAIL)
    store.replace_intake_observed([row(owner=owner.id)], observed_at=T0)
    spy = Spy(mock_with(OWNER_EMAIL))
    run(store, spy)
    assert spy.reconciled()[-1]["owned_by"] == f"{1:032x}"
    doc = json.loads(MOCK.read_text())   # in ServiceNow, the email now belongs to another user
    doc["users"].append({"sys_id": "e" * 32, "user_name": "successor", "email": OWNER_EMAIL})
    successor = parse(doc)
    successor.seed([("github:acme/notes", store.cmdb_record("acme/notes").sys_id, "APM0001001", None)])
    spy.mock = successor
    got = run(store, spy)
    assert (got["synced"], got["unchanged"]) == (1, 0) and spy.reconciled()[-1]["owned_by"] == "e" * 32
    assert store.cmdb_record("acme/notes").number == "APM0001001"
    assert run(store, spy)["unchanged"] == 1


def test_a_rename_that_only_changes_case_updates_the_same_ci(store):
    store.replace_intake_observed([row("acme/notes", schema="intake/v1", owner="team")], observed_at=T0)
    spy = Spy(mock_with())
    run(store, spy)
    store.replace_intake_observed([row("Acme/Notes", schema="intake/v1", owner="team")], observed_at=T0)
    got = run(store, spy)
    assert got["synced"] == 1 and spy.reconciled()[-1]["correlation_id"] == "github:acme/notes"
    assert spy.reconciled()[-1]["name"] == "Acme/Notes"
    [record] = store.cmdb_records()
    assert (record.number, record.last_operation) == ("APM0001001", "UPDATE")


def test_a_new_intake_file_with_the_same_values_is_reconciled_once_as_no_change(store):
    """A renewal with the same values is a new intake file (a new blob): it is sent once, ServiceNow answers NO_CHANGE, and the record
    gets a fresh last_synced_at, so the request's Registered in ServiceNow step is done rather than waiting for good."""
    owner = person(store, "o", OWNER_EMAIL)
    store.replace_intake_observed([row(owner=owner.id)], observed_at=T0)
    spy = Spy(mock_with(OWNER_EMAIL))
    run(store, spy)
    later = T0 + dt.timedelta(days=1)
    store.replace_intake_observed([row(owner=owner.id, blob_sha="d" * 40)], observed_at=later)
    got = run(store, spy, now=later)
    assert (got["synced"], got["unchanged"]) == (1, 0) and len(spy.reconciled()) == 2
    notes = store.cmdb_record("acme/notes")
    assert (notes.number, notes.last_operation, notes.last_synced_at) == ("APM0001001", "NO_CHANGE", later)
    assert run(store, spy, now=later)["unchanged"] == 1   # and only once


# ---- 0.7.0: the CI's lifecycle fields follow the portal (spec 2026-11-02 "ServiceNow write-back", rule 9) ----

def applied(store, action, repo="acme/notes"):
    """A lifecycle request for the repo by an admin, applied in git as the writer does: its steps done, then apply_lifecycle."""
    from portal.store.base import ACTION_STEPS
    admin = person(store, "admin", "admin@example.test")
    req = lifecycle_request(store, {"admin": admin}, T0, action=action, repo=repo)
    store.add_lifecycle_steps(request_id=req.id, steps=ACTION_STEPS[action], now=T0)
    for step in ACTION_STEPS[action]:
        store.move_lifecycle_step(request_id=req.id, step=step, to_state="done", now=T0)
    store.apply_lifecycle(request_id=req.id, merge_sha=None, note="Applied in git.", now=T0)
    return req


def synced(store, spy):
    """A good first sync of acme/notes (an intake/v1 record: no owner to look up) and its CI's sys_id."""
    store.replace_intake_observed([row(schema="intake/v1", owner="team")], observed_at=T0)
    assert run(store, spy)["synced"] == 1
    return store.cmdb_record("acme/notes").sys_id


def corrections(store):
    return [e for e in store.lifecycle_events() if e.step == "ci_corrected"]


def test_a_suspended_app_s_ci_is_non_operational_with_its_task(store):
    spy = Spy(mock_with())
    sys_id = synced(store, spy)
    assert spy.mock.client().ci_lifecycle(sys_id) == {"operational_status": "1", "install_status": "1", "u_golden_road_state": "active",
                                                      "u_golden_road_task": ""}
    applied(store, "suspend")
    got = run(store, spy)
    assert (got["synced"], got["corrected"]) == (1, 0) and store.cmdb_record("acme/notes").last_operation == "UPDATE"
    assert spy.mock.client().ci_lifecycle(sys_id) == {"operational_status": "2", "install_status": "1", "u_golden_road_state": "suspended",
                                                      "u_golden_road_task": "SCTASK0010001"}
    assert run(store, spy)["unchanged"] == 1 and corrections(store) == []


def test_a_resumed_app_s_ci_is_operational_again_with_no_task(store):
    spy = Spy(mock_with())
    sys_id = synced(store, spy)
    applied(store, "suspend")
    run(store, spy)
    applied(store, "resume")
    assert run(store, spy)["synced"] == 1
    assert spy.mock.client().ci_lifecycle(sys_id) == {"operational_status": "1", "install_status": "1", "u_golden_road_state": "active",
                                                      "u_golden_road_task": ""}


def test_a_decommissioned_app_s_ci_is_retired(store):
    spy = Spy(mock_with())
    sys_id = synced(store, spy)
    applied(store, "decommission")
    assert run(store, spy)["synced"] == 1
    assert spy.mock.client().ci_lifecycle(sys_id) == {"operational_status": "6", "install_status": "7",
                                                      "u_golden_road_state": "decommissioned", "u_golden_road_task": "SCTASK0010001"}


def test_an_edit_in_servicenow_is_written_back_and_noted(store):
    spy = Spy(mock_with())
    sys_id = synced(store, spy)
    req = applied(store, "suspend")
    run(store, spy)
    spy.mock.edit_ci("acme/notes", u_golden_road_state="active", operational_status="1")
    got = run(store, spy)
    assert (got["synced"], got["corrected"], got["unchanged"]) == (1, 1, 0) and store.cmdb_record("acme/notes").last_operation == "UPDATE"
    assert spy.mock.client().ci_lifecycle(sys_id)["u_golden_road_state"] == "suspended"
    [event] = corrections(store)
    assert event.request_id == req.id and event.sent_at is None
    assert event.note == ("acme/notes's CI lifecycle fields were changed in ServiceNow (operational_status, u_golden_road_state). The "
                          "collector wrote back the portal's state, suspended for SCTASK0010001: the platform's state is what it applied in "
                          "git (rule 9).")
    again = run(store, spy)
    assert (again["unchanged"], again["corrected"]) == (1, 0) and len(corrections(store)) == 1


def test_a_value_servicenow_holds_is_never_echoed_in_the_note(store):
    spy = Spy(mock_with())
    synced(store, spy)
    applied(store, "decommission")
    run(store, spy)
    spy.mock.edit_ci("acme/notes", u_golden_road_state="<b>active</b>", u_golden_road_task="SCTASK9999999 please")
    assert spy.mock.client().ci_lifecycle(store.cmdb_record("acme/notes").sys_id)["u_golden_road_state"] == UNEXPECTED
    assert run(store, spy)["corrected"] == 1
    [event] = corrections(store)
    assert "(u_golden_road_state, u_golden_road_task)" in event.note and "decommissioned for SCTASK0010001" in event.note
    assert "<b>" not in event.note and "SCTASK9999999" not in event.note and UNEXPECTED not in event.note


def test_an_edit_of_an_app_never_acted_on_is_written_back_without_a_note(store):
    spy = Spy(mock_with())
    sys_id = synced(store, spy)
    spy.mock.edit_ci("acme/notes", install_status="7")
    assert run(store, spy)["corrected"] == 1
    assert spy.mock.client().ci_lifecycle(sys_id)["install_status"] == "1"
    assert store.lifecycle_events() == []


def test_a_restarted_mock_reports_nothing_and_corrects_nothing(store):
    spy = Spy(mock_with())
    synced(store, spy)
    applied(store, "suspend")
    run(store, spy)
    restarted = mock_with()
    seed_mock(restarted, store)
    spy.mock = restarted
    got = run(store, spy)
    assert (got["unchanged"], got["corrected"], got["synced"]) == (1, 0, 0) and corrections(store) == []


def test_a_write_back_servicenow_doesn_t_keep_is_refused_and_never_noted(store):
    """The reconcile answers, but the CI still reads the edit (a rule or an ACL on the instance): no note says it was written back, and
    the record shows cmdb_refused."""
    spy = Spy(mock_with())
    sys_id = synced(store, spy)
    applied(store, "suspend")
    run(store, spy)
    spy.mock.edit_ci("acme/notes", u_golden_road_state="active")

    def kept_edit(request, body):
        if request.method == "GET" and request.url.path.endswith(sys_id) and "u_golden_road_state" in request.url.params["sysparm_fields"]:
            return httpx.Response(200, json={"result": {"sys_id": sys_id, "u_golden_road_state": "active"}})
        return None
    spy.answer = kept_edit
    for _ in range(3):  # every pass: the record stays refused, never flips to synced, and nothing is noted (I1)
        got = run(store, spy)
        assert (got["refused"], got["corrected"], got["synced"], got["problem"]) == (1, 0, 0, "cmdb_refused")
        assert store.cmdb_record("acme/notes").last_error == "cmdb_refused" and corrections(store) == []
    spy.mock.edit_ci("acme/notes", u_golden_road_state="active")  # the CI still holds the edit ...
    spy.answer = None  # ... and the instance keeps the write-back now: synced, corrected and noted once
    got = run(store, spy)
    assert (got["synced"], got["corrected"], got["problem"]) == (1, 1, None) and store.cmdb_record("acme/notes").last_error is None
    assert len(corrections(store)) == 1 and run(store, spy)["unchanged"] == 1


def test_a_refused_record_whose_ci_holds_the_portal_s_fields_is_sent_again_and_cleared(store):
    """A refusal with the hash unchanged (the IRE refused a retry) is retried while the CI reads right: the error is cleared by a good
    sync, never left behind an unchanged count."""
    spy = Spy(mock_with())
    synced(store, spy)
    store.record_cmdb_error(repo="acme/notes", error="cmdb_refused", now=T0)
    got = run(store, spy)
    assert (got["synced"], got["unchanged"], got["corrected"]) == (1, 0, 0) and store.cmdb_record("acme/notes").last_error is None


def two_synced(store, spy):
    store.replace_intake_observed([row("acme/a", schema="intake/v1", owner="team"), row("acme/b", schema="intake/v1", owner="team")],
                                  observed_at=T0)
    assert run(store, spy)["synced"] == 2
    return store.cmdb_record("acme/a"), store.cmdb_record("acme/b")


def test_reading_the_ci_s_fields_when_servicenow_is_down_stops_the_pass(store):
    spy = Spy(mock_with())
    _, b = two_synced(store, spy)
    store.replace_intake_observed([row("acme/a", schema="intake/v1", owner="team"),
                                   row("acme/b", schema="intake/v1", owner="team", tier="high")], observed_at=T0)
    spy.answer = lambda request, body: httpx.Response(503, json={}) if request.method == "GET" else None
    sent = len(spy.reconciled())
    got = run(store, spy)
    assert (got["unreachable"], got["synced"], got["problem"]) == (1, 0, "cmdb_unreachable")
    assert store.cmdb_record("acme/a").last_error == "cmdb_unreachable"
    assert store.cmdb_record("acme/b") == b and len(spy.reconciled()) == sent   # the second row is untouched: never sent, never marked
    spy.answer = None
    again = run(store, spy)
    assert (again["synced"], again["unreachable"], again["problem"]) == (2, 0, None)
    assert [r.last_error for r in store.cmdb_records()] == [None, None] and spy.reconciled()[-1]["u_tier"] == "high"


def test_a_ci_deleted_in_servicenow_is_that_row_s_refusal_and_the_next_row_still_syncs(store):
    """A 4xx on one CI (here a 404: someone deleted it) is that row's error; the pass goes on, and the next pass reconciles it again
    (the instance creates it anew)."""
    spy = Spy(mock_with())
    two_synced(store, spy)
    del spy.mock._cis["github:acme/a"]
    store.replace_intake_observed([row("acme/a", schema="intake/v1", owner="team"),
                                   row("acme/b", schema="intake/v1", owner="team", tier="high")], observed_at=T0)
    got = run(store, spy)
    assert (got["refused"], got["synced"], got["unreachable"], got["problem"]) == (1, 1, 0, "cmdb_refused")
    assert [(r.repo, r.last_error) for r in store.cmdb_records()] == [("acme/a", "cmdb_refused"), ("acme/b", None)]
    again = run(store, spy)
    assert (again["synced"], again["unchanged"], again["problem"]) == (1, 1, None)
    a = store.cmdb_record("acme/a")
    assert (a.last_operation, a.number, a.last_error) == ("INSERT", "APM0001003", None)


@pytest.mark.parametrize("status", [401, 429])
def test_a_4xx_about_the_integration_itself_still_stops_the_pass(store, status):
    """401 (the integration user's credentials), 407 and 429 (rate limited) are about every CI, not one: the pass stops there."""
    spy = Spy(mock_with())
    two_synced(store, spy)
    spy.mock.edit_ci("acme/a", install_status="7")
    spy.answer = lambda request, body: httpx.Response(status, json={}) if request.method == "GET" else None
    got = run(store, spy)
    assert (got["unreachable"], got["refused"], got["problem"]) == (1, 0, "cmdb_unreachable")
    assert store.cmdb_record("acme/b").last_error is None


def test_the_correction_note_is_added_before_the_sync_is_recorded(store):
    """A pass that stops between the two has the note (a second one may follow on the next pass: at least once), never a sync without
    its note."""
    spy = Spy(mock_with())
    synced(store, spy)
    applied(store, "suspend")
    run(store, spy)
    spy.mock.edit_ci("acme/notes", u_golden_road_state="active")

    def crash(**k):
        raise RuntimeError("the collector stopped")
    real, store.record_cmdb_sync = store.record_cmdb_sync, crash
    with pytest.raises(RuntimeError):
        run(store, spy)
    assert len(corrections(store)) == 1
    store.record_cmdb_sync = real


def test_a_correction_note_the_store_refuses_is_that_row_s_error_and_the_pass_goes_on(store):
    from portal.store.base import Conflict
    spy = Spy(mock_with())
    two_synced(store, spy)
    applied(store, "suspend", repo="acme/a")
    applied(store, "suspend", repo="acme/b")
    run(store, spy)
    spy.mock.edit_ci("acme/a", u_golden_road_state="active")
    spy.mock.edit_ci("acme/b", u_golden_road_state="active")
    real = store.add_lifecycle_event

    def refuse_a(**k):
        if "acme/a's" in k["note"]:
            raise Conflict("wrong_state")
        return real(**k)
    store.add_lifecycle_event = refuse_a
    got = run(store, spy)
    assert (got["refused"], got["synced"], got["corrected"], got["problem"]) == (1, 1, 1, "cmdb_refused")
    assert [(r.repo, r.last_error) for r in store.cmdb_records()] == [("acme/a", "cmdb_refused"), ("acme/b", None)]
    assert [e.note.split("'s CI")[0] for e in corrections(store)] == ["acme/b"]


def test_the_hash_follows_the_lifecycle_state(store):
    base = row(owner="p_" + "1" * 32)
    assert fields_hash(base, "o@example.test", "a" * 32, WEB) == fields_hash(base, "o@example.test", "a" * 32, WEB, None)
    req = applied(store, "suspend")
    assert store.lifecycle_state("acme/notes").request == req.id
    assert fields_hash(base, "o@example.test", "a" * 32, WEB, store.lifecycle_state("acme/notes")) != fields_hash(
        base, "o@example.test", "a" * 32, WEB)


# ---- the final review: an owner who left (I-2) ----

def owner_gone(store):
    """acme/notes (intake/v2) synced with its owner, then the owner leaves: ServiceNow has no user with their email any more."""
    owner = person(store, "o", OWNER_EMAIL)
    store.replace_intake_observed([row(owner=owner.id)], observed_at=T0)
    spy = Spy(mock_with(OWNER_EMAIL))
    assert run(store, spy)["synced"] == 1
    spy.mock.users = ()
    return spy, store.cmdb_record("acme/notes").sys_id


@pytest.mark.parametrize("action, want", [
    ("decommission", {"operational_status": "6", "install_status": "7", "u_golden_road_state": "decommissioned",
                      "u_golden_road_task": "SCTASK0010001"}),
    ("suspend", {"operational_status": "2", "install_status": "1", "u_golden_road_state": "suspended", "u_golden_road_task": "SCTASK0010001"})])
def test_an_owner_who_left_still_has_the_ci_follow_the_lifecycle_without_owned_by(store, action, want):
    spy, sys_id = owner_gone(store)
    applied(store, action)
    got = run(store, spy)
    assert (got["owner_unknown"], got["synced"], got["problem"]) == (1, 0, "cmdb_owner_unknown")
    assert spy.mock.client().ci_lifecycle(sys_id) == want
    assert "owned_by" not in spy.reconciled()[-1] and spy.reconciled()[-1]["u_golden_road_state"] == want["u_golden_road_state"]
    kept = store.cmdb_record("acme/notes")
    assert (kept.sys_id, kept.number, kept.last_error) == (sys_id, "APM0001001", "cmdb_owner_unknown"), "the warning stays"
    assert corrections(store) == [], "the portal's own state change is not an edit in ServiceNow"
    sent = len(spy.reconciled())
    again = run(store, spy)
    assert (again["owner_unknown"], len(spy.reconciled())) == (1, sent), "nothing to send again"
    assert store.cmdb_record("acme/notes").last_error == "cmdb_owner_unknown"


def test_an_edit_in_servicenow_of_an_owner_who_left_is_written_back_and_noted(store):
    spy, sys_id = owner_gone(store)
    req = applied(store, "decommission")
    run(store, spy)
    spy.mock.edit_ci("acme/notes", u_golden_road_state="active", operational_status="1")
    got = run(store, spy)
    assert (got["owner_unknown"], got["corrected"]) == (1, 1)
    assert spy.mock.client().ci_lifecycle(sys_id)["u_golden_road_state"] == "decommissioned"
    [event] = corrections(store)
    assert event.request_id == req.id and "(operational_status, u_golden_road_state)" in event.note
    assert store.cmdb_record("acme/notes").last_error == "cmdb_owner_unknown"
    assert run(store, spy)["corrected"] == 0 and len(corrections(store)) == 1


def test_an_owner_who_left_before_any_ci_still_gets_none(store):
    nobody = person(store, "n", None)
    store.replace_intake_observed([row(owner=nobody.id)], observed_at=T0)
    applied(store, "decommission")
    spy = Spy(mock_with())
    assert run(store, spy)["owner_unknown"] == 1 and spy.reconciled() == []


def test_an_owner_who_left_and_a_ci_servicenow_refuses_to_change_is_refused(store):
    spy, sys_id = owner_gone(store)
    applied(store, "decommission")
    spy.answer = lambda request, body: httpx.Response(200, json={"result": {"items": [{"className": "cmdb_ci_business_app",
                                                                                         "errors": [{"error": "X", "message": "x"}]}]}}) \
        if body else None
    got = run(store, spy)
    assert (got["refused"], got["problem"]) == (1, "cmdb_refused") and store.cmdb_record("acme/notes").last_error == "cmdb_refused"


def test_an_owner_who_left_and_whose_ci_was_deleted_stays_owner_unknown(store):
    spy, sys_id = owner_gone(store)
    spy.mock._cis.clear()
    applied(store, "decommission")
    got = run(store, spy)
    assert (got["owner_unknown"], got["refused"]) == (1, 0) and len(spy.reconciled()) == 1, "no CI is made without its owner"
