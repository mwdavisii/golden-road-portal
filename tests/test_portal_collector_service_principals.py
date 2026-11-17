"""The collector's service principal step (spec 2026-11-16 S4, S5, S10): open a task (never two), read it, create the principal, note
the task; every failure kept with its attempt and error and tried again on the next pass, with no attempt cap."""

import contextlib
import datetime as dt

import httpx
import pytest
from portal_fakes import BASE, SP_APPROVED, SP_PENDING, T0, mock_servicenow, sp_request

from portal.collectors.service_principals import collect_service_principals
from portal.directory_client import DirectoryClientError, MockDirectoryClient
from portal.graph_scopes import load_mock
from portal.servicenow import Client
from portal.servicenow_mock import PASSWORD, ci_sys_id
from portal.store.memory import MemoryStore

CATALOG = load_mock()


def world():
    store = MemoryStore()
    owner = store.sign_in(issuer="i", subject="o", display_name="Owner", email="owner@example.test", groups=[], roles=[], now=T0,
                          max_age=dt.timedelta(hours=8))[0]
    store.record_cmdb_sync(repo="acme/notes", sys_id=ci_sys_id("acme/notes"), number="APM0001001", fields_hash="f" * 64, operation="INSERT",
                           now=T0)
    return store, owner


def run(store, servicenow, directory=None, now=T0):
    return collect_service_principals(store, servicenow, directory or MockDirectoryClient(), base_url=BASE, now=now, catalog=CATALOG)


class Flaky:
    """The mock behind a transport that answers 503 to the first `failures` requests matching `method path`."""

    def __init__(self, mock, failures, method, path):
        self.mock, self.failures, self.method, self.path = mock, failures, method, path

    def client(self):
        inner = self.mock.transport()

        def handle(request):
            if request.method == self.method and request.url.path.startswith(self.path) and self.failures > 0:
                self.failures -= 1
                return httpx.Response(503, json={"error": {"message": "down"}, "status": "failure"})
            return inner.handle_request(request)
        return Client(self.mock.instance, http=httpx.Client(transport=httpx.MockTransport(handle)), auth=(self.mock.integration_user, PASSWORD))


def test_pre_approved_scopes_are_created_on_the_next_pass_without_servicenow():
    store, owner = world()
    req = sp_request(store, owner.id, T0)
    counts = run(store, None)
    assert counts["created"] == 1 and counts["failed"] == 0
    [made] = store.service_principals("acme/notes")
    assert (made.display_name, made.scopes, made.request_id) == ("sp-notes-calendar", ("Calendars.Read",), req.id)
    assert store.service_principal_request(req.id).state == "created" and run(store, None)["created"] == 0


def test_a_pending_request_waits_without_servicenow():
    store, owner = world()
    req = sp_request(store, owner.id, T0, SP_PENDING)
    assert run(store, None)["waiting"] == 1 and store.service_principal_request(req.id).task_number is None


def test_a_request_opens_its_task_waits_and_is_created_once_approved_then_notes_the_task():
    store, owner = world()
    mock = mock_servicenow()
    req = sp_request(store, owner.id, T0, SP_PENDING)
    first = run(store, mock)
    assert (first["opened"], first["waiting"]) == (1, 1)
    view = store.service_principal_request(req.id)
    assert view.task_number == "SCTASK0010007" and view.state == "pending"
    with mock.client() as c:
        task = c.task("SCTASK0010007")
    assert task.ci == ci_sys_id("acme/notes") and "Calendars.ReadWrite (needs approval)" in mock.task_description("SCTASK0010007")
    assert mock.task_fields("SCTASK0010007")["short_description"] == "Service principal for acme/notes: sp-notes-rooms"
    assert run(store, mock)["waiting"] == 1   # still open
    mock.decide_task("SCTASK0010007", "3", "")
    second = run(store, mock)
    assert (second["approved"], second["created"], second["noted"]) == (1, 1, 1)
    [made] = store.service_principals()
    assert mock.task_history("SCTASK0010007")[-1] == ("work_notes", f"Service principal sp-notes-rooms created: client ID {made.client_id}. "
                                                                    f"Link it to test or prod on the app's page.\n\nGolden road portal request: "
                                                                    f"{BASE}/requests/{req.id}")
    assert store.service_principal_request(req.id).note_sent_at == T0
    assert run(store, mock)["noted"] == 0   # noted once


@pytest.mark.parametrize("state", ["4", "7"])
def test_a_task_closed_incomplete_or_skipped_rejects_the_request_with_its_close_notes(state):
    store, owner = world()
    mock = mock_servicenow()
    req = sp_request(store, owner.id, T0, SP_PENDING)
    run(store, mock)
    mock.decide_task("SCTASK0010007", state, "Use Calendars.Read: " + "x" * 3000)
    assert run(store, mock)["rejected"] == 1
    view = store.service_principal_request(req.id)
    assert view.state == "rejected" and view.close_notes.startswith("Use Calendars.Read: ") and len(view.close_notes) == 2000
    assert store.service_principals() == []


class Stopped(BaseException):
    """The collector's process ending mid-pass (a kill, a node drain): the step catches Exception per request, never this."""


def test_a_pass_that_stops_after_opening_the_task_never_opens_a_second(monkeypatch):
    store, owner = world()
    mock = mock_servicenow()
    req = sp_request(store, owner.id, T0, SP_PENDING)
    real = store.record_service_principal_task

    def crash(**kwargs):
        raise Stopped("the collector stopped here")   # the process ends: not an Exception, so no failure is recorded either
    monkeypatch.setattr(store, "record_service_principal_task", crash)
    with pytest.raises(Stopped):
        run(store, mock)
    monkeypatch.setattr(store, "record_service_principal_task", real)
    assert run(store, mock)["opened"] == 1
    assert store.service_principal_request(req.id).task_number == "SCTASK0010007" and mock.task_fields("SCTASK0010008") is None


def test_an_outage_is_retried_every_pass_with_no_cap():
    store, owner = world()
    mock = mock_servicenow()
    req = sp_request(store, owner.id, T0, SP_PENDING)
    flaky = Flaky(mock, 25, "POST", "/api/now/table/sc_task")
    for n in range(1, 26):
        assert run(store, flaky)["failed"] == 1
        view = store.service_principal_request(req.id)
        assert (view.state, view.attempts, view.last_error, view.task_number) == ("pending", n, "ServiceNow answered 503 for the new task", None)
    assert run(store, flaky)["opened"] == 1


def test_a_directory_failure_keeps_the_request_approved_and_is_retried():
    store, owner = world()
    req = sp_request(store, owner.id, T0)

    class Down:
        def create_principal(self, display_name, scopes):
            raise DirectoryClientError("the directory is down")
    assert run(store, None, Down())["failed"] == 1
    view = store.service_principal_request(req.id)
    assert (view.state, view.attempts, view.last_error) == ("approved", 1, "the directory is down")
    assert run(store, None)["created"] == 1


@pytest.mark.parametrize("scopes, error", [
    (["Calendars.Read", "Calendars.ReadWrite"], "Calendars.ReadWrite needs approval and the request has no ServiceNow task: not created"),
    (["Directory.ReadWrite.All"], "Directory.ReadWrite.All is not in the scope catalog: not created"),
])
def test_an_approved_request_the_catalog_doesn_t_allow_is_never_created(scopes, error):
    """Decided while planning: item 28 (review I1). The store takes an approved request whatever its tiers (the web decides them); the
    collector, the only creator, checks them again: a requestable scope needs a task that approved it, and every scope is catalogued."""
    store, owner = world()
    req = sp_request(store, owner.id, T0, {**SP_APPROVED, "scopes": scopes})   # approved, no justification, no task
    for n in (1, 2):
        counts = run(store, mock_servicenow())
        assert (counts["created"], counts["failed"]) == (0, 1)
        view = store.service_principal_request(req.id)
        assert (view.state, view.attempts, view.last_error) == ("approved", n, error)
    assert store.service_principals() == []


def test_a_requestable_scope_approved_by_its_task_is_created():
    store, owner = world()
    mock = mock_servicenow()
    req = sp_request(store, owner.id, T0, SP_PENDING)
    run(store, mock)
    mock.decide_task("SCTASK0010007", "3", "")
    assert run(store, mock)["created"] == 1 and store.service_principal_request(req.id).attempts == 0


def test_a_failed_note_is_sent_on_the_next_pass():
    store, owner = world()
    mock = mock_servicenow()
    req = sp_request(store, owner.id, T0, SP_PENDING)
    run(store, mock)
    mock.decide_task("SCTASK0010007", "3", "")
    flaky = Flaky(mock, 1, "PATCH", "/api/now/table/sc_task/")
    counts = run(store, flaky)
    assert (counts["created"], counts["noted"], counts["failed"]) == (1, 0, 1)
    assert store.service_principal_request(req.id).note_sent_at is None
    assert run(store, flaky)["noted"] == 1


def test_a_task_gone_from_servicenow_is_a_failure_not_a_decision():
    store, owner = world()
    req = sp_request(store, owner.id, T0, SP_PENDING)
    store.record_service_principal_task(request_id=req.id, task_number="SCTASK0010099", task_sys_id="9" * 32, now=T0)
    assert run(store, mock_servicenow())["failed"] == 1
    assert store.service_principal_request(req.id).last_error == "ServiceNow has no task SCTASK0010099"


def test_another_pass_holding_the_lock_is_busy():
    store, owner = world()
    sp_request(store, owner.id, T0)

    @contextlib.contextmanager
    def held():
        yield False
    store.service_principal_lock = held
    assert run(store, None)["busy"] is True and store.service_principals() == []


def test_without_a_catalog_nothing_is_created():
    """Decided while planning: item 28's third case: a collector with no catalog can't check the tiers, so it creates nothing."""
    store, owner = world()
    req = sp_request(store, owner.id, T0)
    counts = collect_service_principals(store, None, MockDirectoryClient(), base_url=BASE, now=T0, catalog=None)
    assert (counts["created"], counts["failed"]) == (0, 1)
    view = store.service_principal_request(req.id)
    assert (view.state, view.attempts, view.last_error) == ("approved", 1, "the collector has no scope catalog: not created")


def test_a_task_a_lifecycle_request_cites_is_never_recorded_on_a_service_principal_request():
    """Round 3 review I1, the collector's side: a lifecycle request's completion closes its task Closed Complete, so the task the step
    found or opened is refused when a lifecycle request cites it (the store's task_taken), recorded as a failure and tried again."""
    from portal_fakes import lifecycle_request
    store, owner = world()
    admin = store.sign_in(issuer="i", subject="a", display_name="Admin", email="admin@example.test", groups=[], roles=["admin"], now=T0,
                          max_age=dt.timedelta(hours=8))[0]
    lifecycle_request(store, {"admin": admin}, T0, task="SCTASK0010007", claim=False)
    req = sp_request(store, owner.id, T0, SP_PENDING)
    mock = mock_servicenow()
    for n in (1, 2):
        counts = run(store, mock)
        assert (counts["opened"], counts["failed"]) == (0, 1)
        view = store.service_principal_request(req.id)
        assert (view.task_number, view.attempts, view.last_error) == (None, n, "the store refused the task: task_taken")
    assert mock.task_fields("SCTASK0010008") is None   # the task it opened was found again, never a second one


def test_a_task_someone_else_opened_with_the_request_s_id_never_blocks_the_portal_s_own():
    """Round 3 review I2 and final review M1: the request id is on the request page; a task another user opened (and perhaps closed)
    with it is never the request's, and find_task's query asks only for the portal's own, so the portal opens its task as usual."""
    store, owner = world()
    req = sp_request(store, owner.id, T0, SP_PENDING)
    mock = mock_servicenow()
    mock.add_task(number="SCTASK0010007", sys_id="7" * 32, correlation_id=req.id, short_description="mine", description="x", ci=None,
                  created_by="someone")
    mock.decide_task("SCTASK0010007", "3", "")
    counts = run(store, mock)
    assert (counts["opened"], counts["failed"]) == (1, 0)
    view = store.service_principal_request(req.id)
    assert (view.state, view.task_number) == ("pending", "SCTASK0010008")
    assert run(store, mock)["opened"] == 0   # found again, never a second


# ---- Round 3 review I3: one bad item never stalls the step ----

class Surrogate:
    """The mock behind a transport that answers each task's "SURROGATE" in its close notes as a lone surrogate, escaped as a JSON
    answer may carry it (the mock itself can't encode one)."""

    def __init__(self, mock):
        self.mock = mock

    def client(self):
        import json
        inner = self.mock.transport()

        def handle(request):
            answer = inner.handle_request(request)
            if request.method != "GET":
                return answer
            answer.read()
            body = json.dumps(json.loads(answer.content)).replace("SURROGATE", "\\ud800")
            return httpx.Response(answer.status_code, content=body.encode(), headers={"content-type": "application/json"})
        return Client(self.mock.instance, http=httpx.Client(transport=httpx.MockTransport(handle)), auth=(self.mock.integration_user, PASSWORD))


def test_close_notes_lose_control_format_and_surrogate_characters_before_they_are_cut_and_kept():
    store, owner = world()
    mock = mock_servicenow()
    req = sp_request(store, owner.id, T0, SP_PENDING)
    run(store, mock)
    mock.decide_task("SCTASK0010007", "4", "Use\u0000 Calendars.ReadSURROGATE \u202einstead\n[code]x[/code]" + "y" * 3000)
    assert run(store, Surrogate(mock))["rejected"] == 1
    notes = store.service_principal_request(req.id).close_notes
    assert notes.startswith("Use Calendars.Read instead\n[\u200bcode]x[\u200b/code]") and len(notes) == 2000
    assert not any(ord(c) < 32 and c != "\n" or 0xD800 <= ord(c) <= 0xDFFF or c == "\u202e" for c in notes)


def test_one_request_that_raises_doesn_t_stop_another_s_creation():
    store, owner = world()
    bad, good = sp_request(store, owner.id, T0), sp_request(store, owner.id, T0, {**SP_APPROVED, "display_name": "sp-notes-mail"})

    class Picky(MockDirectoryClient):
        def create_principal(self, display_name, scopes):
            if display_name == "sp-notes-calendar":
                raise KeyError("an instance's own text")
            return super().create_principal(display_name, scopes)
    for n in (1, 2):
        counts = run(store, None, Picky())
        assert (counts["created"], counts["failed"]) == ((1, 1) if n == 1 else (0, 1))
        view = store.service_principal_request(bad.id)
        assert (view.state, view.attempts, view.last_error) == ("approved", n, "the step stopped on KeyError: will retry")
    assert store.service_principal_request(good.id).state == "created"


def test_one_request_that_raises_doesn_t_stop_another_s_task_or_the_later_steps(monkeypatch):
    store, owner = world()
    mock = mock_servicenow()
    bad = sp_request(store, owner.id, T0, SP_PENDING)
    good = sp_request(store, owner.id, T0, {**SP_PENDING, "repo": "acme/other", "display_name": "sp-other-rooms"})
    approved = sp_request(store, owner.id, T0, {**SP_APPROVED, "repo": "acme/other", "display_name": "sp-other-calendar"})
    real = store.cmdb_record
    monkeypatch.setattr(store, "cmdb_record", lambda repo: (_ for _ in ()).throw(RuntimeError("x")) if repo == "acme/notes" else real(repo))
    counts = run(store, mock)
    assert (counts["opened"], counts["created"], counts["failed"]) == (1, 1, 1)
    assert store.service_principal_request(bad.id).last_error == "the step stopped on RuntimeError: will retry"
    assert store.service_principal_request(good.id).task_number == "SCTASK0010007"
    assert store.service_principal_request(approved.id).state == "created"


def test_a_replay_that_fails_still_creates_the_pre_approved_and_the_tasks_wait():
    from portal.servicenow_mock import MockServiceNowError
    store, owner = world()
    approved, pending = sp_request(store, owner.id, T0), sp_request(store, owner.id, T0, SP_PENDING)

    class Broken:
        def client(self):
            raise MockServiceNowError("a replayed task is named twice")
    counts = run(store, Broken())
    assert (counts["created"], counts["waiting"], counts["servicenow"]) == (1, 1, "MockServiceNowError")
    assert store.service_principal_request(approved.id).state == "created"
    assert store.service_principal_request(pending.id).attempts == 0


def test_a_store_that_can_t_record_a_failure_stops_the_pass_and_records_nothing(monkeypatch):
    """Round 3 re-review: _failed catches only the store's Conflict; a store error (the database gone) is the part's failure (logged by
    type in __main__), never swallowed as if the attempt were kept."""
    import psycopg
    store, owner = world()
    req = sp_request(store, owner.id, T0)

    class Down:
        def create_principal(self, display_name, scopes):
            raise DirectoryClientError("the directory is down")

    def gone(**kwargs):
        raise psycopg.OperationalError("the database is gone")
    monkeypatch.setattr(store, "record_service_principal_failure", gone)
    with pytest.raises(psycopg.OperationalError):
        run(store, None, Down())
    view = store.service_principal_request(req.id)
    assert (view.state, view.attempts, view.last_error) == ("approved", 0, None)


def test_an_app_decommissioned_after_its_request_gets_no_principal():
    """Final review I1: the collector checks the app's lifecycle state before it creates (a failure, retried; a person must look)."""
    store, owner = world()
    req = sp_request(store, owner.id, T0)
    real = store.lifecycle_state
    store.lifecycle_state = lambda repo: type("State", (), {"state": "decommissioned"})() if repo.lower() == "acme/notes" else real(repo)
    counts = run(store, None)
    assert (counts["created"], counts["failed"]) == (0, 1)
    view = store.service_principal_request(req.id)
    assert (view.state, view.last_error) == ("approved", "acme/notes is decommissioned: not created")


# ---- final review I2: the requester never approves their own task ----

def requester(store, email):
    return store.sign_in(issuer="i", subject=email, display_name="Requester", email=email, groups=[], roles=[], now=T0,
                         max_age=dt.timedelta(hours=8))[0]


def test_a_task_closed_complete_by_the_requester_s_own_servicenow_user_is_not_approved():
    store, _ = world()
    me = requester(store, "mwdavisii@example.com")   # a mock ServiceNow user
    req = sp_request(store, me.id, T0, SP_PENDING)
    mock = mock_servicenow()
    run(store, mock)
    mock.decide_task("SCTASK0010007", "3", "", decided_by_email="MWDavisii@example.com")
    for n in (1, 2):
        counts = run(store, mock)
        assert (counts["approved"], counts["failed"]) == (0, 1)
        view = store.service_principal_request(req.id)
        assert (view.state, view.attempts, view.last_error) == ("pending", n, "the requester closed the task: not approved")
    assert store.service_principals() == []


def test_a_task_closed_complete_by_no_one_is_not_approved():
    import json

    from portal.servicenow_mock import MOCK
    doc = json.loads(MOCK.read_text(encoding="utf-8"))
    doc["users"] = [u for u in doc["users"] if u["user_name"] != "mock-approver"]   # the decision then names no closer
    store, owner = world()
    req = sp_request(store, owner.id, T0, SP_PENDING)
    mock = mock_servicenow(doc=doc)
    run(store, mock)
    mock.decide_task("SCTASK0010007", "3", "")
    assert run(store, mock)["failed"] == 1
    view = store.service_principal_request(req.id)
    assert (view.state, view.last_error) == ("pending", "the task names no one who closed it: not approved")


def test_a_task_closed_complete_by_another_user_is_approved_and_a_rejection_needs_no_closer_check():
    store, _ = world()
    me = requester(store, "mwdavisii@example.com")
    approved = sp_request(store, me.id, T0, SP_PENDING)
    rejected = sp_request(store, me.id, T0, {**SP_PENDING, "display_name": "sp-notes-mail", "scopes": ["Mail.Send"]})
    mock = mock_servicenow()
    run(store, mock)
    yes, no = (store.service_principal_request(r.id).task_number for r in (approved, rejected))   # same time: either order
    mock.decide_task(yes, "3", "", decided_by_email="other@example.com")
    mock.decide_task(no, "4", "No.", decided_by_email="mwdavisii@example.com")
    counts = run(store, mock)
    assert (counts["approved"], counts["rejected"], counts["created"], counts["failed"]) == (1, 1, 1, 0)
    assert store.service_principal_request(rejected.id).state == "rejected"
    assert store.service_principal_request(approved.id).state == "created"


def test_a_requester_with_no_email_on_record_is_never_approved_by_a_task():
    store, owner = world()
    req = sp_request(store, owner.id, T0, SP_PENDING)
    mock = mock_servicenow()
    run(store, mock)
    mock.decide_task("SCTASK0010007", "3", "")
    store.person_email = lambda person_id: None
    assert run(store, mock)["failed"] == 1
    assert store.service_principal_request(req.id).last_error == (
        "the requester has no email to check against the task's closer: not approved")
