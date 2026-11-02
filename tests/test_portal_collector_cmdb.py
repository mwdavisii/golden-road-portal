"""The collector's CMDB part (spec 2026-10-31 N1-N4): each readable intake record that is new, changed since its last good sync, or whose
last attempt failed is sent to ServiceNow as a business application; unchanged ones are not; an unknown owner is never sent; ServiceNow
down stops the pass, shows the problem, and the next pass catches up."""

import datetime as dt
import json

import httpx
import pytest
from portal_fakes import T0

from portal.collectors.cmdb import SHORT_MAX, collect_cmdb, fields_hash, seed_mock
from portal.servicenow import Client
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
                      "u_review_by": "2027-10-31", "owned_by": f"{1:032x}"}
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
