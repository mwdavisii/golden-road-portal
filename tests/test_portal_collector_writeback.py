"""The collector's write-back (spec 2026-11-02 L10, L11): the outbox goes to the ServiceNow tasks per request in order, each note with the
portal's request link; completed closes the task; a failure keeps the note and holds back that request's later ones, with no attempt
cap; a note unsent for more than a day is stale."""

import datetime as dt

import httpx
from portal_fakes import BASE, GROUPS, ISSUER, T0, lifecycle_request, mock_servicenow

from portal.collectors.writeback import collect_writeback
from portal.outbox import STALE_AFTER, note_text, stale_events
from portal.roles import roles_for
from portal.servicenow import ServiceNowError
from portal.store.base import Conflict
from portal.store.memory import MemoryStore


def world():
    store, people = MemoryStore(), {}
    for who, groups in GROUPS.items():
        if groups is not None:
            people[who] = store.sign_in(issuer=ISSUER, subject=f"sub-{who}", display_name=who.title(), email=f"{who}@example.test",
                                        groups=groups, roles=roles_for(groups, {"admin": frozenset({"golden-road-admins"})}), now=T0,
                                        max_age=dt.timedelta(hours=8))[0]
    return store, people


def test_notes_go_in_order_with_the_request_link_and_completed_closes_the_task():
    store, people = world()
    req = lifecycle_request(store, people, T0)
    for step, note in (("pr_opened", "Pull request #1 opened."), ("merged", "Merged.")):
        store.add_lifecycle_event(request_id=req.id, step=step, note=note, now=T0)
    store.add_lifecycle_steps(request_id=req.id, steps=("deploy",), now=T0)
    store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="done", now=T0)
    store.apply_lifecycle(request_id=req.id, merge_sha=None, note="Applied in git.", now=T0)
    mock = mock_servicenow()
    assert collect_writeback(store, mock, base_url=BASE, now=T0) == {"sent": 4, "failed": 0, "waiting": 0}
    link = f"\n\nGolden road portal request: {BASE}/requests/{req.id}"
    assert mock.task_history("SCTASK0010001") == [("work_notes", "Requested in the golden road portal by Admin: suspend the app." + link),
                                                  ("work_notes", "Pull request #1 opened." + link), ("work_notes", "Merged." + link),
                                                  ("closed", "Applied in git." + link)]
    assert mock.task_fields("SCTASK0010001")["open"] is False
    assert store.lifecycle_events(unsent=True) == [] and all(e.sent_at == T0 for e in store.lifecycle_events())
    assert collect_writeback(store, mock, base_url=BASE, now=T0) == {"sent": 0, "failed": 0, "waiting": 0}


def test_an_abandoned_request_s_task_gets_its_note_and_stays_open():
    store, people = world()
    req = lifecycle_request(store, people, T0)
    store.add_lifecycle_steps(request_id=req.id, steps=("deploy",), now=T0)
    store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="closed", now=T0)
    command = store.add_lifecycle_command(request_id=req.id, command="abandon", actor_id=people["admin"].id, now=T0)
    store.abandon_lifecycle(request_id=req.id, command_id=command.id, note="Abandoned by Admin. The task stays open.", now=T0)
    mock = mock_servicenow()
    assert collect_writeback(store, mock, base_url=BASE, now=T0)["sent"] == 2
    assert [kind for kind, _ in mock.task_history("SCTASK0010001")] == ["work_notes", "work_notes"]
    assert mock.task_fields("SCTASK0010001")["open"] is True


def test_an_outage_in_the_middle_keeps_the_order_and_retries_without_a_cap():
    store, people = world()
    req = lifecycle_request(store, people, T0)
    store.add_lifecycle_event(request_id=req.id, step="pr_opened", note="Pull request #1 opened.", now=T0)
    mock, calls = mock_servicenow(), {"n": 0}
    inner = mock.transport()

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 2 or 3 <= calls["n"] <= 7:  # the second note fails, then five passes of outage
            return httpx.Response(503, json={"error": {"message": "Instance hibernating: the instance's own words"}})
        return inner.handle_request(request)
    mock.transport = lambda: httpx.MockTransport(flaky)
    assert collect_writeback(store, mock, base_url=BASE, now=T0) == {"sent": 1, "failed": 1, "waiting": 0}
    for _ in range(5):
        assert collect_writeback(store, mock, base_url=BASE, now=T0)["failed"] == 1
    [kept] = store.lifecycle_events(unsent=True)
    assert (kept.step, kept.attempts, kept.last_error) == ("pr_opened", 6, "ServiceNow answered 503 for the task's work note")
    assert collect_writeback(store, mock, base_url=BASE, now=T0)["sent"] == 1
    link = f"\n\nGolden road portal request: {BASE}/requests/{req.id}"
    assert mock.task_history("SCTASK0010001") == [("work_notes", "Requested in the golden road portal by Admin: suspend the app." + link),
                                                  ("work_notes", "Pull request #1 opened." + link)]
    assert store.lifecycle_events(unsent=True) == []


def test_a_failed_note_holds_back_the_request_s_later_notes_not_another_request_s():
    store, people = world()
    first = lifecycle_request(store, people, T0)
    store.add_lifecycle_event(request_id=first.id, step="pr_opened", note="Pull request #1 opened.", now=T0)
    second = lifecycle_request(store, people, T0, repo="acme/tools")
    mock = mock_servicenow()
    real = mock.client

    def client():
        c = real()
        add = c.add_work_note

        def refuse_first(sys_id, note):
            if first.id in note and "Requested" in note:
                raise ServiceNowError("ServiceNow answered 500 for the task's work note")
            return add(sys_id, note)
        c.add_work_note = refuse_first
        return c
    mock.client = client
    assert collect_writeback(store, mock, base_url=BASE, now=T0) == {"sent": 1, "failed": 1, "waiting": 1}
    assert [e.request_id for e in store.lifecycle_events(unsent=True)] == [first.id, first.id]
    assert [e.attempts for e in store.lifecycle_events(unsent=True)] == [1, 0]
    assert store.lifecycle_events(second.id)[0].sent_at == T0


def test_a_close_that_fails_is_kept_and_sent_on_the_next_pass():
    store, people = world()
    req = lifecycle_request(store, people, T0)
    store.add_lifecycle_steps(request_id=req.id, steps=("deploy",), now=T0)
    store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="done", now=T0)
    store.apply_lifecycle(request_id=req.id, merge_sha=None, note="Applied in git.", now=T0)
    mock = mock_servicenow()
    inner = mock.transport()
    down = {"on": True}

    def refuse_close(request):
        if down["on"] and b'"close_notes"' in request.content:
            return httpx.Response(502, json={})
        return inner.handle_request(request)
    mock.transport = lambda: httpx.MockTransport(refuse_close)
    assert collect_writeback(store, mock, base_url=BASE, now=T0) == {"sent": 1, "failed": 1, "waiting": 0}
    [kept] = store.lifecycle_events(unsent=True)
    assert (kept.step, kept.last_error) == ("completed", "ServiceNow answered 502 for the task's close")
    down["on"] = False
    assert collect_writeback(store, mock, base_url=BASE, now=T0)["sent"] == 1
    assert mock.task_fields("SCTASK0010001")["open"] is False


def test_a_note_unsent_for_more_than_a_day_is_stale():
    store, people = world()
    req = lifecycle_request(store, people, T0)
    assert stale_events(store, T0 + STALE_AFTER) == []
    assert [e.request_id for e in stale_events(store, T0 + STALE_AFTER + dt.timedelta(seconds=1))] == [req.id]
    collect_writeback(store, mock_servicenow(), base_url=BASE, now=T0)
    assert stale_events(store, T0 + 2 * STALE_AFTER) == []


def test_the_link_is_the_portal_s_request_page():
    store, people = world()
    req = lifecycle_request(store, people, T0)
    [event] = store.lifecycle_events(req.id)
    assert note_text(event, "https://portal.example.test/").endswith(f"\n\nGolden road portal request: https://portal.example.test/requests/{req.id}")


def test_a_note_opens_no_servicenow_markup_and_carries_no_invisible_or_bidi_character():
    """An admin's reason and an IdP display name reach the note: no [code] tag (any case) may make ServiceNow render HTML, and no control
    or format character (a bidi override, a zero-width one) may make the note read other than it is. Newlines stay."""
    store, people = world()
    req = lifecycle_request(store, people, T0)
    reason = ("Patched.\n[code]<a href=\"https://evil.example\">ok</a>[/code] [CODE]x[/Code] [ code ]"
              "\u202eevil\u202c\u2066x\u2069\u200b\u200d\u2060\ufeff\x07\t\r end")
    store.add_lifecycle_event(request_id=req.id, step="pr_opened", note=reason, now=T0)
    mock = mock_servicenow()
    collect_writeback(store, mock, base_url=BASE, now=T0)
    sent = mock.task_history("SCTASK0010001")[-1][1]
    assert "[code]" not in sent.lower() and "[/code]" not in sent.lower() and "[ code ]" not in sent.lower()
    assert "[\u200bcode]<a href" in sent and "[\u200b/code]" in sent and "[\u200bCODE]" in sent and "[\u200b/Code]" in sent
    invisible = [c for c in sent.replace("[\u200b", "[") if c != "\n" and __import__("unicodedata").category(c) in ("Cc", "Cf")]
    assert invisible == []
    assert sent.startswith("Patched.\n") and "evilx end" in sent and sent.endswith(f"/requests/{req.id}")


def test_a_note_servicenow_refuses_on_a_task_closed_there_stays_unsent_holds_back_the_rest_and_is_pending_after_a_day():
    """A person closed the task in ServiceNow, and the instance refuses work notes on a closed task (the mock's option): the note stays
    unsent, retried every pass with no cap, its request's later notes wait behind it, and after a day the Re-review page says so."""
    from portal.web.routes_rereview import cmdb_problems
    store, people = world()
    req = lifecycle_request(store, people, T0)
    store.add_lifecycle_event(request_id=req.id, step="pr_opened", note="Pull request #1 opened.", now=T0)
    mock = mock_servicenow()
    mock.refuse_notes_on_closed = True
    with mock.client() as client:
        client.close_task("5ca1ab1e000040008000000000010001", "Closed by a person in ServiceNow.")
    for attempt in range(1, 5):
        assert collect_writeback(store, mock, base_url=BASE, now=T0) == {"sent": 0, "failed": 1, "waiting": 1}
        first, later = store.lifecycle_events(unsent=True)
        assert (first.step, first.attempts, first.last_error) == ("requested", attempt, "ServiceNow answered 403 for the task's work note")
        assert (later.step, later.attempts, later.sent_at) == ("pr_opened", 0, None)
    assert mock.task_history("SCTASK0010001") == [("closed", "Closed by a person in ServiceNow.")]
    assert cmdb_problems(store, {}, T0 + STALE_AFTER)[0] == []
    problems, _, stale = cmdb_problems(store, {}, T0 + STALE_AFTER + dt.timedelta(seconds=1))
    assert [p for p, _ in problems] == ["cmdb_writeback_pending"] and [e.step for e in stale] == ["requested", "pr_opened"]


def test_by_default_the_mock_takes_a_work_note_on_a_closed_task():
    store, people = world()
    lifecycle_request(store, people, T0)
    mock = mock_servicenow()
    with mock.client() as client:
        client.close_task("5ca1ab1e000040008000000000010001", "Closed by a person in ServiceNow.")
    assert collect_writeback(store, mock, base_url=BASE, now=T0)["sent"] == 1
    assert mock.fresh().refuse_notes_on_closed is False
    mock.refuse_notes_on_closed = True
    assert mock.fresh().refuse_notes_on_closed is True


def test_a_pass_that_can_t_take_the_write_back_lock_sends_nothing():
    """Two collectors, or a --once beside the pod: one pass sends the outbox at a time (Postgres's advisory lock; the MemoryStore's is
    always free)."""
    import contextlib
    store, people = world()
    lifecycle_request(store, people, T0)
    with store.writeback_lock() as held:
        assert held is True
    store.writeback_lock = lambda: contextlib.nullcontext(False)
    mock = mock_servicenow()
    assert collect_writeback(store, mock, base_url=BASE, now=T0) == {"sent": 0, "failed": 0, "waiting": 0, "busy": True}
    assert mock.task_history("SCTASK0010001") == [] and len(store.lifecycle_events(unsent=True)) == 1


def test_a_note_another_pass_marked_sent_first_stops_its_request_not_the_pass():
    store, people = world()
    first = lifecycle_request(store, people, T0)
    store.add_lifecycle_event(request_id=first.id, step="pr_opened", note="Pull request #1 opened.", now=T0)
    second = lifecycle_request(store, people, T0, repo="acme/tools")
    real = store.mark_lifecycle_event_sent

    def raced(*, event_id, now):
        if store.lifecycle_events(first.id)[0].id == event_id:
            real(event_id=event_id, now=now)  # the other pass marked it ...
            raise Conflict("wrong_state")  # ... so this one's mark is refused
        return real(event_id=event_id, now=now)
    store.mark_lifecycle_event_sent = raced
    mock = mock_servicenow()
    got = collect_writeback(store, mock, base_url=BASE, now=T0)
    assert got == {"sent": 1, "failed": 0, "waiting": 0}
    assert store.lifecycle_events(second.id)[0].sent_at == T0
    assert [e.step for e in store.lifecycle_events(unsent=True)] == ["pr_opened"]   # left to the pass that sent the first one, or the next
