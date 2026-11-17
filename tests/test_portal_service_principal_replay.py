"""What a service principal request's ServiceNow task and note say (spec 2026-11-16 "Each collector pass"), and the replay that lets
the web's and the collector's fresh mocks see the tasks the portal opened and the decisions the web stored (S6, "The mock")."""

import datetime as dt

from portal_fakes import BASE, SP_PENDING, T0, mock_servicenow, sp_request

from portal import service_principals as sp
from portal.graph_scopes import load_mock
from portal.servicenow_mock import ci_sys_id
from portal.store.memory import MemoryStore

CATALOG = load_mock()


def world():
    store = MemoryStore()
    owner = store.sign_in(issuer="i", subject="o", display_name="Owner", email="owner@example.test", groups=[], roles=[], now=T0,
                          max_age=dt.timedelta(hours=8))[0]
    admin = store.sign_in(issuer="i", subject="a", display_name="Admin", email="admin@example.test", groups=[], roles=["admin"], now=T0,
                          max_age=dt.timedelta(hours=8))[0]
    return store, owner, admin


def test_the_task_says_what_was_asked_by_whom_and_links_the_request():
    store, owner, _ = world()
    req = store.service_principal_request(sp_request(store, owner.id, T0, SP_PENDING).id)
    assert sp.task_short_description(req) == "Service principal for acme/notes: sp-notes-rooms"
    text = sp.task_description(req, "owner@example.test", BASE, CATALOG)
    assert text == ("Requested in the golden road portal by owner@example.test for acme/notes.\n\nName: sp-notes-rooms\nScopes:\n"
                    "- Calendars.Read\n- Calendars.ReadWrite (needs approval)\n\nJustification:\nBooks meeting rooms for the team.\n\n"
                    f"Golden road portal request: {BASE}/requests/{req.id}")
    assert "a person with no email on record" in sp.task_description(req, None, BASE, CATALOG)


def test_the_text_is_neutralised_and_cut_to_the_field_lengths():
    store, owner, _ = world()
    req = store.service_principal_request(sp_request(store, owner.id, T0, {**SP_PENDING, "justification": "[code]<b>x</b>[/code]‮"}).id)
    text = sp.task_description(req, "owner@example.test", BASE, CATALOG)
    assert "[code]" not in text and "‮" not in text and len(text) <= 4000
    long = store.service_principal_request(sp_request(store, owner.id, T0, {**SP_PENDING, "repo": "acme/" + "n" * 100,
                                                                             "display_name": "sp-" + "n" * 64}).id)
    assert len(sp.task_short_description(long)) == 160


def test_a_scope_no_longer_in_the_catalog_is_marked():
    store, owner, _ = world()
    req = store.service_principal_request(sp_request(store, owner.id, T0, {**SP_PENDING, "scopes": ["Calendars.ReadWrite", "Sites.Read.All"]}).id)
    assert "- Sites.Read.All (no longer in the catalog)" in sp.task_description(req, None, BASE, CATALOG)


def test_the_creation_note_names_the_client_id_and_the_request():
    store, owner, _ = world()
    req = sp_request(store, owner.id, T0)
    made = store.create_service_principal(request_id=req.id, principal_id="sp-" + "a" * 24, client_id="0e2f6c1d-3b4a-4c5d-8e6f-7a8b9c0d1e2f",
                                          now=T0)
    assert sp.creation_note(store.service_principal_request(req.id), made, BASE) == (
        "Service principal sp-notes-calendar created: client ID 0e2f6c1d-3b4a-4c5d-8e6f-7a8b9c0d1e2f. Link it to test or prod on the app's "
        f"page.\n\nGolden road portal request: {BASE}/requests/{req.id}")


def test_a_fresh_mock_gets_the_store_s_tasks_decisions_and_notes():
    store, owner, admin = world()
    store.record_cmdb_sync(repo="acme/notes", sys_id=ci_sys_id("acme/notes"), number="APM0001001", fields_hash="f" * 64, operation="INSERT",
                           now=T0)
    waiting = sp_request(store, owner.id, T0, SP_PENDING)
    done = sp_request(store, owner.id, T0, {**SP_PENDING, "display_name": "sp-notes-mail", "scopes": ["Mail.Send"]})
    store.record_service_principal_task(request_id=waiting.id, task_number="SCTASK0010007", task_sys_id="7" * 32, now=T0)
    store.record_service_principal_task(request_id=done.id, task_number="SCTASK0010008", task_sys_id="8" * 32, now=T0)
    store.add_mock_task_decision(task_number="SCTASK0010008", state="3", close_notes="", actor_id=admin.id, now=T0)
    store.decide_service_principal_request(request_id=done.id, approved=True, close_notes=None, now=T0)
    made = store.create_service_principal(request_id=done.id, principal_id="sp-" + "a" * 24, client_id="0e2f6c1d-3b4a-4c5d-8e6f-7a8b9c0d1e2f",
                                          now=T0)
    store.mark_service_principal_note_sent(request_id=done.id, now=T0)
    store.add_mock_task_decision(task_number="SCTASK0010007", state="4", close_notes="Use Calendars.Read.", actor_id=admin.id, now=T0)
    mock = mock_servicenow()
    fresh = mock.fresh()
    sp.replay_into(fresh, store, BASE, CATALOG)
    with fresh.client() as c:
        rejected, approved = c.task("SCTASK0010007"), c.task("SCTASK0010008")
        assert (rejected.state, rejected.close_notes, rejected.ci) == ("4", "Use Calendars.Read.", ci_sys_id("acme/notes"))
        assert approved.state == "3" and c.find_task(waiting.id).number == "SCTASK0010007"
        assert c.create_task(short_description="x", description="y", correlation_id="r_" + "c" * 32).number == "SCTASK0010009"
    assert fresh.task_history("SCTASK0010008") == [("closed", ""), ("work_notes", sp.creation_note(
        store.service_principal_request(done.id), made, BASE))]
    assert fresh.task_description("SCTASK0010007") == sp.task_description(store.service_principal_request(waiting.id), "owner@example.test",
                                                                           BASE, CATALOG)
    assert mock.task_fields("SCTASK0010007") is None   # the settings' mock is never written to


def test_replayed_hands_out_a_fresh_replayed_mock_per_client():
    store, owner, admin = world()
    req = sp_request(store, owner.id, T0, SP_PENDING)
    replayed = sp.Replayed(mock_servicenow(), store, BASE, CATALOG)
    with replayed.client() as c:
        ref = c.create_task(short_description="x", description="y", correlation_id=req.id)
    store.record_service_principal_task(request_id=req.id, task_number=ref.number, task_sys_id=ref.sys_id, now=T0)
    store.add_mock_task_decision(task_number=ref.number, state="3", close_notes="", actor_id=admin.id, now=T0)
    with replayed.client() as c:
        assert c.task(ref.number).state == "3"


def test_a_request_whose_task_the_fresh_mock_holds_already_is_skipped_and_logged(caplog):
    """Round 5 review: a stored task number or sys_id that clashes with the mock's own (a fixture task's) is skipped with its
    decision and its note, never a 'named twice' error for every page and every collector pass; the log names the request only."""
    store, owner, admin = world()
    by_number = sp_request(store, owner.id, T0, SP_PENDING)
    store.record_service_principal_task(request_id=by_number.id, task_number="SCTASK0010001", task_sys_id="8" * 32, now=T0)
    store.add_mock_task_decision(task_number="SCTASK0010001", state="4", close_notes="Not this one.", actor_id=admin.id, now=T0)
    by_sys_id = sp_request(store, owner.id, T0, {**SP_PENDING, "display_name": "sp-notes-mail"})
    store.record_service_principal_task(request_id=by_sys_id.id, task_number="SCTASK0010009",
                                        task_sys_id="5ca1ab1e000040008000000000010002", now=T0)
    kept = sp_request(store, owner.id, T0, {**SP_PENDING, "display_name": "sp-notes-ok"})
    store.record_service_principal_task(request_id=kept.id, task_number="SCTASK0010008", task_sys_id="9" * 32, now=T0)
    mock = mock_servicenow().fresh()
    with caplog.at_level("WARNING", logger="portal.service_principals"):
        sp.replay_into(mock, store, BASE, CATALOG)
    assert mock.task_fields("SCTASK0010001")["open"] and mock.task_history("SCTASK0010001") == []   # the fixture task, undecided
    assert mock.task_fields("SCTASK0010009") is None and mock.task_fields("SCTASK0010008") is not None
    logged = [r.getMessage() for r in caplog.records if r.name == "portal.service_principals"]
    assert len(logged) == 2 and by_number.id in logged[0] and by_sys_id.id in logged[1]
    assert not any("SCTASK" in m or "sp-notes" in m for m in logged)
    assert sp.Replayed(mock_servicenow(), store, BASE, CATALOG).client() is not None


def test_a_replayed_decision_is_closed_by_the_decider_s_servicenow_user():
    """Final review I2: the web stores who decided; the replay closes the task as their mock user (by email), else the mock approver."""
    from portal.servicenow_mock import APPROVER_EMAIL
    store, owner, admin = world()
    known = store.sign_in(issuer="i", subject="k", display_name="Known", email="MWDavisii@example.com", groups=[], roles=["admin"], now=T0,
                          max_age=dt.timedelta(hours=8))[0]
    first = sp_request(store, owner.id, T0, SP_PENDING)
    second = sp_request(store, owner.id, T0, {**SP_PENDING, "display_name": "sp-notes-mail", "scopes": ["Mail.Send"]})
    store.record_service_principal_task(request_id=first.id, task_number="SCTASK0010007", task_sys_id="7" * 32, now=T0)
    store.record_service_principal_task(request_id=second.id, task_number="SCTASK0010008", task_sys_id="8" * 32, now=T0)
    store.add_mock_task_decision(task_number="SCTASK0010007", state="3", close_notes="", actor_id=known.id, now=T0)
    store.add_mock_task_decision(task_number="SCTASK0010008", state="3", close_notes="", actor_id=admin.id, now=T0)
    fresh = mock_servicenow().fresh()
    sp.replay_into(fresh, store, BASE, CATALOG)
    with fresh.client() as c:
        assert c.task("SCTASK0010007").closed_by == c.user_sys_id("mwdavisii@example.com")
        assert c.task("SCTASK0010008").closed_by == c.user_sys_id(APPROVER_EMAIL)
