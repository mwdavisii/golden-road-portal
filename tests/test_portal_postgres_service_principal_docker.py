"""Migration 0013 (spec 2026-11-16): the service_principal kind and its draft, its own insert and update triggers (the collector's
moves only), the principal that is the one its request asked for, the mock decision (once, never the requester's), the view the
collector reads through, the grants, each process role's work through PostgresStore, and 0013 on a populated database."""

import datetime as dt
import uuid

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.types.json import Jsonb
from portal_fakes import SP_APPROVED, SP_PENDING
from portal_pg import pg_db, pg_server  # noqa: F401

pytestmark = pytest.mark.docker
NOW = dt.datetime(2026, 11, 16, 12, tzinfo=dt.UTC)
INSERT = ("INSERT INTO request (id, kind, repo, state, draft, requested_by, created_at, updated_at) "
          "VALUES (%s, 'service_principal', %s, %s, %s, %s, now(), now())")
TASK, TASK_SYS_ID = "SCTASK0010007", "7" * 32
SP_ID, CLIENT_ID = "sp-" + "a" * 24, "0e2f6c1d-3b4a-4c5d-8e6f-7a8b9c0d1e2f"


def conn_as(pg_db, role):
    return psycopg.connect(pg_db(role), autocommit=True)


def person(pg_db, subject):
    from portal.store.postgres import PostgresStore
    store = PostgresStore(pg_db("portal_web"))
    try:
        return store.sign_in(issuer="i", subject=subject, display_name=subject, email=f"{subject}@example.test", groups=[], roles=[],
                             now=NOW, max_age=dt.timedelta(hours=8))[0].id
    finally:
        store.close()


def rid():
    return "r_" + uuid.uuid4().hex


def sp(c, pid, draft=SP_APPROVED, state=None, request_id=None, repo="acme/notes"):
    """The request's repo is given, not the draft's: a draft naming another repo is refused."""
    request_id = request_id or rid()
    c.execute(INSERT, (request_id, repo, state or ("pending" if "justification" in draft else "approved"), Jsonb(draft), pid))
    return request_id


@pytest.mark.parametrize("draft, state", [
    (SP_APPROVED, "pending"),                                                     # pending needs its justification
    (SP_PENDING, "approved"),                                                     # approved has none
    (SP_APPROVED, "created"),
    ({**SP_APPROVED, "scopes": ["Contacts.Read", "Calendars.Read"]}, "approved"),  # not sorted
    ({**SP_APPROVED, "scopes": ["Calendars.Read", "Calendars.Read"]}, "approved"),
    ({**SP_APPROVED, "scopes": []}, "approved"),
    ({**SP_APPROVED, "scopes": [1]}, "approved"),
    ({**SP_APPROVED, "scopes": ["calendars.read"]}, "approved"),
    ({**SP_APPROVED, "display_name": "sp-Notes"}, "approved"),
    ({**SP_APPROVED, "repo": "acme/other"}, "approved"),
    ({**SP_APPROVED, "schema": "service-principal-request/v2"}, "approved"),
    ({**SP_APPROVED, "extra": 1}, "approved"),
    ({**SP_PENDING, "justification": " padded "}, "pending"),
    ({**SP_PENDING, "justification": "x" * 2001}, "pending"),
])
def test_a_service_principal_request_starts_approved_or_pending_with_its_draft(pg_db, draft, state):
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        with pytest.raises(errors.RaiseException):
            sp(c, pid, draft, state)
        sp(c, pid, SP_APPROVED)
        sp(c, pid, SP_PENDING)


def test_a_scope_may_start_with_a_lowercase_letter(pg_db):
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        sp(c, pid, {**SP_APPROVED, "scopes": ["Calendars.Read", "eDiscovery.Read.All"]})
        with pytest.raises(errors.RaiseException):
            sp(c, pid, {**SP_APPROVED, "display_name": "sp-notes-x", "scopes": ["eDiscovery.read.All"]})


def test_no_two_open_requests_share_a_name_in_any_case_and_a_created_name_is_taken_for_good(pg_db):
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        first = sp(c, pid)
        with pytest.raises(errors.UniqueViolation):
            sp(c, pid)
    from portal.store.postgres import PostgresStore
    collector = PostgresStore(pg_db("portal_collector"))
    try:
        collector.create_service_principal(request_id=first, principal_id=SP_ID, client_id=CLIENT_ID, now=NOW)
    finally:
        collector.close()
    with conn_as(pg_db, "portal_web") as c:
        with pytest.raises(errors.RaiseException, match="already used"):
            sp(c, pid)


def web_insert(pg_db, pid):
    with conn_as(pg_db, "portal_web") as c:
        return sp(c, pid)

NAME_LOCK_SPACE = 0x67726E6D  # 0013's two-key namespace for a name's lock ("grnm")


def name_lock(c, name):
    c.execute("SELECT pg_advisory_xact_lock(%s, hashtext(lower(%s)))", (NAME_LOCK_SPACE, name))


def my_advisory_locks(c):
    return c.execute("SELECT classid::bigint, objid::bigint, objsubid FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
                     "AND granted").fetchall()


def test_a_name_s_lock_is_two_keys_so_no_name_can_take_the_collector_s_session_locks(pg_db):
    """N1: a one-key lock on hashtext(name) shares the key space of SP_LOCK and WRITEBACK_LOCK, so a name searched for that hash would
    hang the collector's pass on its own session lock. A two-key lock (objsubid 2) never meets a one-key one (objsubid 1)."""
    from portal.store.postgres import SP_LOCK, WRITEBACK_LOCK
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        first = sp(c, pid)
    with conn_as(pg_db, "portal_collector") as session:
        session.execute("SELECT pg_advisory_lock(%s), pg_advisory_lock(%s)", (SP_LOCK, WRITEBACK_LOCK))
        with psycopg.connect(pg_db("portal_web")) as web:   # one transaction: the insert guard's lock is held until it ends
            sp(web, pid, {**SP_APPROVED, "display_name": "sp-notes-other"})
            assert [(k, s) for k, _, s in my_advisory_locks(web)] == [(NAME_LOCK_SPACE, 2)]
            web.rollback()
        with psycopg.connect(pg_db("portal_collector")) as creating:
            creating.execute("INSERT INTO service_principal (id, repo, display_name, client_id, scopes, request_id, created_at) "
                             "VALUES (%s, 'acme/notes', 'sp-notes-calendar', %s, %s, %s, now())", (SP_ID, CLIENT_ID, ["Calendars.Read"], first))
            assert [(k, s) for k, _, s in my_advisory_locks(creating)] == [(NAME_LOCK_SPACE, 2)]
            creating.rollback()


def wait_for_a_lock_waiter(pg_db, locktype):
    """Until a session waits for a lock of this type (pg_locks: granted false). Bounded so a broken test fails instead of hanging."""
    import time
    with conn_as(pg_db, "postgres") as c:
        for _ in range(600):
            if c.execute("SELECT count(*) FROM pg_locks WHERE NOT granted AND locktype = %s", (locktype,)).fetchone()[0]:
                return
            time.sleep(0.05)
    raise AssertionError(f"no session waited for a {locktype} lock")


def in_thread(work):
    import threading
    out = {}

    def run():
        try:
            out["value"] = work()
        except Exception as exc:  # noqa: BLE001 - the test looks at it
            out["error"] = exc
    thread = threading.Thread(target=run)
    thread.start()
    return thread, out


def test_a_request_s_insert_waits_for_its_name_s_lock(pg_db):
    """M1: the insert guard takes the name's transaction advisory lock before it looks for a created principal of that name."""
    pid = person(pg_db, "owner")
    with psycopg.connect(pg_db("portal_owner")) as holder:
        name_lock(holder, "SP-Notes-Calendar")   # any case: the lock is the lowercase name's
        thread, out = in_thread(lambda: web_insert(pg_db, pid))
        wait_for_a_lock_waiter(pg_db, "advisory")
        holder.rollback()
    thread.join()
    assert "error" not in out, out


def test_a_principal_s_creation_waits_for_its_name_s_lock(pg_db):
    from portal.store.postgres import PostgresStore
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        request_id = sp(c, pid)
    collector = PostgresStore(pg_db("portal_collector"))
    try:
        with psycopg.connect(pg_db("portal_owner")) as holder:
            name_lock(holder, "sp-notes-calendar")
            thread, out = in_thread(lambda: collector.create_service_principal(request_id=request_id, principal_id=SP_ID, client_id=CLIENT_ID,
                                                                               now=NOW))
            wait_for_a_lock_waiter(pg_db, "advisory")
            holder.rollback()
        thread.join()
        assert "error" not in out and out["value"].display_name == "sp-notes-calendar"
    finally:
        collector.close()


def test_a_request_inserted_while_its_name_is_being_created_is_refused(pg_db):
    """M1's race: request A is being created (its principal inserted, A moving to created, not committed) while request B of the same
    name is inserted. Without the lock B's insert waited only for A's row, then went in once A left the open states: open for good with
    a name a principal has (its create would fail every pass)."""
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        first = sp(c, pid)
    with psycopg.connect(pg_db("portal_collector")) as creating:
        creating.execute("INSERT INTO service_principal (id, repo, display_name, client_id, scopes, request_id, created_at) "
                         "VALUES (%s, 'acme/notes', 'sp-notes-calendar', %s, %s, %s, now())", (SP_ID, CLIENT_ID, ["Calendars.Read"], first))
        creating.execute("UPDATE request SET state = 'created' WHERE id = %s", (first,))
        thread, out = in_thread(lambda: web_insert(pg_db, pid))
        with conn_as(pg_db, "postgres") as c:   # B waits (for the name's lock, or without it for A's row)
            import time
            for _ in range(600):
                if c.execute("SELECT count(*) FROM pg_locks WHERE NOT granted").fetchone()[0]:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("the second insert never waited")
        creating.commit()
    thread.join()
    assert isinstance(out.get("error"), errors.RaiseException) and "already used" in str(out["error"]), out

def test_the_web_and_the_writer_can_t_move_a_service_principal_request(pg_db):
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        request_id = sp(c, pid, SP_PENDING)
        with pytest.raises(errors.RaiseException, match="only the collector"):
            c.execute("UPDATE request SET state = 'approved', decided_by = %s, decided_at = now(), decision_groups = '{}', "
                      "decision_note = 'x', final = '{}' WHERE id = %s", (pid, request_id))
    with conn_as(pg_db, "portal_writer") as c:
        with pytest.raises(errors.RaiseException, match="only the collector"):
            c.execute("UPDATE request SET state = 'writing' WHERE id = %s", (request_id,))


def test_only_the_collector_and_the_owner_move_a_service_principal_request(pg_db):
    """An allow-list: any other role (here the server's superuser, standing in for a role added later) is refused."""
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        request_id = sp(c, pid, SP_PENDING)
    with conn_as(pg_db, "postgres") as c:
        with pytest.raises(errors.RaiseException, match="only the collector"):
            c.execute("UPDATE request SET task_number = %s, task_sys_id = %s WHERE id = %s", (TASK, TASK_SYS_ID, request_id))
    with conn_as(pg_db, "portal_owner") as c:
        c.execute("UPDATE request SET task_number = %s, task_sys_id = %s WHERE id = %s", (TASK, TASK_SYS_ID, request_id))

def test_the_collector_reads_the_kind_through_its_view_never_a_draft(pg_db):
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        request_id = sp(c, pid, SP_PENDING)
    with conn_as(pg_db, "portal_collector") as c:
        [row] = c.execute("SELECT id, draft FROM service_principal_request").fetchall()
        assert row[0] == request_id and row[1]["display_name"] == "sp-notes-rooms"
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("SELECT draft FROM request")



def test_no_function_of_the_collector_s_sees_another_kind_s_draft_through_the_view(pg_db):
    """The view is a security barrier: a cheap function the collector writes (pg_temp) is never run on a row the view's filter drops, so
    it can't print a registration's draft."""
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        c.execute("INSERT INTO request (id, kind, repo, state, draft, requested_by, created_at, updated_at) VALUES "
                  "(%s, 'registration', 'acme/tools', 'pending', %s, %s, now(), now())",
                  (rid(), Jsonb({"schema": "registration-draft/v1", "repo": "acme/tools", "purpose": "MARKER-7f3a builds tools.",
                                 "data_classification": "public"}), pid))
        sp(c, pid, SP_PENDING)
    seen = []
    with conn_as(pg_db, "portal_collector") as c:
        c.execute("CREATE FUNCTION pg_temp.leak(j jsonb) RETURNS boolean LANGUAGE plpgsql COST 0.0000001 AS "
                  "$$ BEGIN RAISE NOTICE 'LEAK %', j; RETURN true; END $$")
        c.add_notice_handler(lambda d: seen.append(d.message_primary))
        assert len(c.execute("SELECT id FROM service_principal_request WHERE pg_temp.leak(draft)").fetchall()) == 1
    assert seen and not any("MARKER-7f3a" in m for m in seen), seen

@pytest.mark.parametrize("change, words", [
    ("state = 'approved'", "decided by its task"),
    ("state = 'created'", "illegal service principal request transition"),
    ("attempts = 2, last_error = 'x'", "one more attempt"),
    ("attempts = 1", "one more attempt"),
    ("last_error = 'x'", "with one more attempt"),
    ("close_notes = 'x'", "only when the task rejects"),
    ("note_sent_at = now()", "marked sent once"),
    ("draft = draft || '{\"extra\": 1}'::jsonb", None),   # the collector can't update draft at all
])
def test_the_collector_s_moves_are_held_to_its_rules(pg_db, change, words):
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        request_id = sp(c, pid, SP_PENDING)
    with conn_as(pg_db, "portal_collector") as c:
        if words is None:
            with pytest.raises(errors.InsufficientPrivilege):
                c.execute(sql.Composed([sql.SQL("UPDATE request SET "), sql.SQL(change), sql.SQL(" WHERE id = %s")]), (request_id,))
            return
        with pytest.raises(errors.RaiseException, match=words):
            c.execute(sql.Composed([sql.SQL("UPDATE request SET "), sql.SQL(change), sql.SQL(" WHERE id = %s")]), (request_id,))


def test_the_collector_moves_no_other_kind_of_request(pg_db):
    """0013 grants the collector UPDATE on some of request's columns, for this kind only: a registration, a binding or a lifecycle
    request it can't move (0012's guard allows any web or writer move to a role that is neither)."""
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        registration = rid()
        c.execute("INSERT INTO request (id, kind, repo, state, draft, requested_by, created_at, updated_at) VALUES "
                  "(%s, 'registration', 'acme/tools', 'pending', %s, %s, now(), now())",
                  (registration, Jsonb({"schema": "registration-draft/v1", "repo": "acme/tools", "purpose": "Builds tools.",
                                        "data_classification": "public"}), pid))
    with conn_as(pg_db, "portal_collector") as c:
        for change in ("state = 'sent_back'", "updated_at = now()", "attempts = 1, last_error = 'x'"):
            with pytest.raises(errors.RaiseException, match="only service principal requests"):
                c.execute(sql.Composed([sql.SQL("UPDATE request SET "), sql.SQL(change), sql.SQL(" WHERE id = %s")]), (registration,))

def test_a_task_is_recorded_once_and_a_request_is_created_only_with_its_principal(pg_db):
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        request_id = sp(c, pid, SP_PENDING)
    with conn_as(pg_db, "portal_collector") as c:
        c.execute("UPDATE request SET task_number = %s, task_sys_id = %s WHERE id = %s", (TASK, TASK_SYS_ID, request_id))
        with pytest.raises(errors.RaiseException, match="recorded once"):
            c.execute("UPDATE request SET task_number = 'SCTASK0010008', task_sys_id = %s WHERE id = %s", ("8" * 32, request_id))
        c.execute("UPDATE request SET state = 'approved' WHERE id = %s", (request_id,))
        with pytest.raises(errors.RaiseException, match="only with its service principal"):
            c.execute("UPDATE request SET state = 'created' WHERE id = %s", (request_id,))
        with pytest.raises(errors.RaiseException, match="the one its request asked for"):
            c.execute("INSERT INTO service_principal (id, repo, display_name, client_id, scopes, request_id, created_at) "
                      "VALUES (%s, 'acme/notes', 'sp-notes-other', %s, %s, %s, now())", (SP_ID, CLIENT_ID, ["Calendars.Read"], request_id))


def test_a_principal_s_scopes_are_its_request_s_in_their_order(pg_db):
    pid = person(pg_db, "owner")
    draft = {**SP_APPROVED, "scopes": ["Calendars.Read", "Contacts.Read", "Presence.Read.All"]}
    with conn_as(pg_db, "portal_web") as c:
        request_id = sp(c, pid, draft)
    insert = ("INSERT INTO service_principal (id, repo, display_name, client_id, scopes, request_id, created_at) "
              "VALUES (%s, 'acme/notes', 'sp-notes-calendar', %s, %s, %s, now())")
    with conn_as(pg_db, "portal_collector") as c:
        for wrong in (list(reversed(draft["scopes"])), draft["scopes"][:2]):
            with pytest.raises(errors.RaiseException, match="the one its request asked for"):
                c.execute(insert, (SP_ID, CLIENT_ID, wrong, request_id))
        c.execute(insert, (SP_ID, CLIENT_ID, draft["scopes"], request_id))

def test_nobody_changes_or_deletes_a_principal(pg_db):
    from portal.store.postgres import PostgresStore
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        request_id = sp(c, pid)
    collector = PostgresStore(pg_db("portal_collector"))
    try:
        collector.create_service_principal(request_id=request_id, principal_id=SP_ID, client_id=CLIENT_ID, now=NOW)
    finally:
        collector.close()
    with conn_as(pg_db, "portal_owner") as c:
        for statement in ("UPDATE service_principal SET scopes = '{Mail.Send}'", "DELETE FROM service_principal",
                          "TRUNCATE service_principal"):
            with pytest.raises(errors.RaiseException):
                c.execute(statement)
    for role in ("portal_web", "portal_writer"):
        with conn_as(pg_db, role) as c:
            assert c.execute("SELECT id FROM service_principal").fetchall() == [(SP_ID,)]
            with pytest.raises(errors.InsufficientPrivilege):
                c.execute("INSERT INTO service_principal (id, repo, display_name, client_id, scopes, request_id, created_at) "
                          "VALUES ('sp-' || repeat('b', 24), 'acme/notes', 'sp-notes-b', %s, '{Calendars.Read}', %s, now())",
                          ("1e2f6c1d-3b4a-4c5d-8e6f-7a8b9c0d1e2f", request_id))


def test_a_mock_decision_is_the_web_s_once_and_never_the_requester_s(pg_db):
    owner, admin = person(pg_db, "owner"), person(pg_db, "admin")
    with conn_as(pg_db, "portal_web") as c:
        request_id = sp(c, owner, SP_PENDING)
    with conn_as(pg_db, "portal_collector") as c:
        c.execute("UPDATE request SET task_number = %s, task_sys_id = %s WHERE id = %s", (TASK, TASK_SYS_ID, request_id))
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("INSERT INTO mock_task_decision VALUES (%s, '3', '', %s, now())", (TASK, admin))
    insert = "INSERT INTO mock_task_decision (task_number, state, close_notes, decided_by, decided_at) VALUES (%s, %s, %s, %s, now())"
    with conn_as(pg_db, "portal_web") as c:
        with pytest.raises(errors.RaiseException, match="their own task"):
            c.execute(insert, (TASK, "3", "", owner))
        with pytest.raises(errors.CheckViolation):
            c.execute(insert, (TASK, "4", " ", admin))   # a rejection says why
        with pytest.raises(errors.RaiseException, match="only an open service principal task"):
            c.execute(insert, ("SCTASK0010001", "3", "", admin))
        c.execute(insert, (TASK, "4", "Use Calendars.Read.", admin))
        with pytest.raises(errors.UniqueViolation):
            c.execute(insert, (TASK, "3", "", admin))
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("UPDATE mock_task_decision SET state = '3'")
    with conn_as(pg_db, "portal_owner") as c:
        with pytest.raises(errors.RaiseException, match="decided once"):
            c.execute("UPDATE mock_task_decision SET state = '3'")
    with conn_as(pg_db, "portal_collector") as c:
        assert c.execute("SELECT state FROM mock_task_decision").fetchall() == [("4",)]


def test_the_collector_audits_only_its_service_principal_entries(pg_db):
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        request_id = sp(c, pid)
        registration = rid()
        c.execute("INSERT INTO request (id, kind, repo, state, draft, requested_by, created_at, updated_at) VALUES "
                  "(%s, 'registration', 'acme/tools', 'pending', %s, %s, now(), now())",
                  (registration, Jsonb({"schema": "registration-draft/v1", "repo": "acme/tools", "purpose": "Builds tools.",
                                        "data_classification": "public"}), pid))
    audit = ("INSERT INTO audit_log (at, actor_kind, actor_id, action, subject_kind, subject_id, before, after, reason) "
             "VALUES (now(), %s, NULL, %s, 'request', %s, NULL, NULL, 'x')")
    with conn_as(pg_db, "portal_collector") as c:
        c.execute(audit, ("system", "service_principal.created", request_id))
        for actor, action, subject in (("system", "service_principal.created", registration), ("system", "request.merged", request_id),
                                       ("writer", "service_principal.created", request_id)):
            with pytest.raises(errors.RaiseException, match="service principal requests"):
                c.execute(audit, (actor, action, subject))


def test_each_process_role_does_its_service_principal_work_through_the_store(pg_db):
    from portal.store.base import Conflict
    from portal.store.postgres import PostgresStore
    owner, admin = person(pg_db, "owner"), person(pg_db, "admin")
    web, writer, collector = (PostgresStore(pg_db(role)) for role in ("portal_web", "portal_writer", "portal_collector"))
    try:
        req = web.create_service_principal_request(request_id=rid(), person_id=owner, repo="acme/notes", draft=SP_PENDING, state="pending",
                                                   now=NOW)
        assert writer.claim_approved(now=NOW) is None
        [seen] = collector.service_principal_requests(states=("pending",))
        assert (seen.id, seen.justification) == (req.id, SP_PENDING["justification"])
        collector.record_service_principal_task(request_id=req.id, task_number=TASK, task_sys_id=TASK_SYS_ID, now=NOW)
        collector.record_service_principal_failure(request_id=req.id, error="ServiceNow answered 503 for the task", now=NOW)
        decision = web.add_mock_task_decision(task_number=TASK, state="3", close_notes="", actor_id=admin, now=NOW)
        assert collector.mock_task_decisions() == [decision]
        collector.decide_service_principal_request(request_id=req.id, approved=True, close_notes=None, now=NOW)
        made = collector.create_service_principal(request_id=req.id, principal_id=SP_ID, client_id=CLIENT_ID, now=NOW)
        collector.mark_service_principal_note_sent(request_id=req.id, now=NOW)
        assert web.service_principals("acme/notes") == [made] == writer.service_principals()
        view = web.service_principal_request(req.id)
        assert (view.state, view.attempts, view.task_number, view.note_sent_at) == ("created", 1, TASK, NOW)
        with pytest.raises(Conflict) as err:
            web.add_mock_task_decision(task_number=TASK, state="3", close_notes="", actor_id=admin, now=NOW)
        assert err.value.code == "decided"
        with pytest.raises(errors.InsufficientPrivilege):
            writer.create_service_principal(request_id=req.id, principal_id="sp-" + "c" * 24,
                                            client_id="2e2f6c1d-3b4a-4c5d-8e6f-7a8b9c0d1e2f", now=NOW)
        with collector.service_principal_lock() as held:
            assert held is True
    finally:
        for s in (web, writer, collector):
            s.close()


def test_one_service_principal_pass_at_a_time_on_the_collector_s_advisory_lock(pg_db):
    from portal.store.postgres import PostgresStore
    first, second = PostgresStore(pg_db("portal_collector")), PostgresStore(pg_db("portal_collector"))
    try:
        with first.service_principal_lock() as held:
            assert held is True
            with second.service_principal_lock() as again:
                assert again is False
            with second.writeback_lock() as other:
                assert other is True   # another lock: the write-back is not held up
        with second.service_principal_lock() as held:
            assert held is True
    finally:
        first.close()
        second.close()


def test_the_collector_s_step_runs_as_portal_collector_with_the_web_s_requests_and_decisions(pg_db):
    """The collector step (Task 10) against Postgres as portal_collector: it reads the requests through the view, the requester's email
    and the CI, opens the task in a replayed mock, creates the pre-approved principal, then (after the web's mock decision) the approved
    one and its note, under SP_LOCK; a third pass does nothing again."""
    from portal_fakes import BASE, mock_servicenow

    from portal.collectors.service_principals import collect_service_principals
    from portal.directory_client import MockDirectoryClient
    from portal.graph_scopes import load_mock
    from portal.service_principals import Replayed
    from portal.servicenow_mock import ci_sys_id
    from portal.store.postgres import PostgresStore
    owner, admin = person(pg_db, "owner"), person(pg_db, "admin")
    web, collector = PostgresStore(pg_db("portal_web")), PostgresStore(pg_db("portal_collector"))
    catalog = load_mock()
    try:
        collector.record_cmdb_sync(repo="acme/notes", sys_id=ci_sys_id("acme/notes"), number="APM0001001", fields_hash="f" * 64,
                                   operation="INSERT", now=NOW)
        pending = web.create_service_principal_request(request_id=rid(), person_id=owner, repo="acme/notes", draft=SP_PENDING,
                                                       state="pending", now=NOW)
        approved = web.create_service_principal_request(request_id=rid(), person_id=owner, repo="acme/notes", draft=SP_APPROVED,
                                                        state="approved", now=NOW)
        servicenow = Replayed(mock_servicenow(), collector, BASE, catalog)

        def run():
            return collect_service_principals(collector, servicenow, MockDirectoryClient(), base_url=BASE, now=NOW, catalog=catalog)
        first = run()
        assert {k: first[k] for k in ("created", "opened", "waiting", "failed")} == {"created": 1, "opened": 1, "waiting": 1, "failed": 0}
        view = web.service_principal_request(pending.id)
        assert (view.state, view.task_number) == ("pending", "SCTASK0010007")
        assert web.service_principal_request(approved.id).state == "created"
        web.add_mock_task_decision(task_number="SCTASK0010007", state="3", close_notes="", actor_id=admin, now=NOW)
        second = run()
        assert {k: second[k] for k in ("approved", "created", "noted", "failed")} == {"approved": 1, "created": 1, "noted": 1, "failed": 0}
        assert sorted(p.display_name for p in web.service_principals("acme/notes")) == ["sp-notes-calendar", "sp-notes-rooms"]
        third = run()
        assert all(third[k] == 0 for k in ("created", "opened", "approved", "rejected", "waiting", "noted", "failed"))
        with servicenow.client() as c:
            task = c.task("SCTASK0010007")
        assert (task.state, task.ci) == ("3", ci_sys_id("acme/notes"))
    finally:
        web.close()
        collector.close()


LIFE = {"action": "suspend", "task": "SCTASK0010001", "task_sys_id": "e" * 32, "reason": "Critical vulnerability; suspended until patched"}
LIFE_NOTE = "Requested in the golden road portal by admin: suspend the app. Reason: " + LIFE["reason"]


def test_a_task_is_a_lifecycle_request_s_or_a_service_principal_request_s_never_both(pg_db):
    """Round 3 review I1, in each role: the web can't cite a service principal request's task for a lifecycle request (its completion
    would close the task Closed Complete and approve the request), and the collector can't record a lifecycle request's task on a
    service principal request (it can't read the lifecycle request's final: the guard is SECURITY DEFINER)."""
    from portal.store.base import Conflict
    from portal.store.postgres import PostgresStore
    owner, admin = person(pg_db, "owner"), person(pg_db, "admin")
    web, collector = PostgresStore(pg_db("portal_web")), PostgresStore(pg_db("portal_collector"))
    try:
        first = web.create_service_principal_request(request_id=rid(), person_id=owner, repo="acme/notes", draft=SP_PENDING, state="pending",
                                                     now=NOW)
        collector.record_service_principal_task(request_id=first.id, task_number=TASK, task_sys_id=TASK_SYS_ID, now=NOW)
        for final in ({**LIFE, "task": TASK}, {**LIFE, "task_sys_id": TASK_SYS_ID}):
            with pytest.raises(Conflict) as err:
                web.create_lifecycle(request_id=rid(), person_id=admin, repo="acme/notes", final=final, note=LIFE_NOTE, now=NOW)
            assert err.value.code == "task_taken"
        web.create_lifecycle(request_id=rid(), person_id=admin, repo="acme/other", final=LIFE, note=LIFE_NOTE, now=NOW)
        second = web.create_service_principal_request(request_id=rid(), person_id=owner, repo="acme/notes",
                                                      draft={**SP_PENDING, "display_name": "sp-notes-x"}, state="pending", now=NOW)
        for number, sys_id in ((LIFE["task"], "8" * 32), ("SCTASK0010008", LIFE["task_sys_id"])):
            with pytest.raises(Conflict) as err:
                collector.record_service_principal_task(request_id=second.id, task_number=number, task_sys_id=sys_id, now=NOW)
            assert err.value.code == "task_taken"
        collector.record_service_principal_task(request_id=second.id, task_number="SCTASK0010008", task_sys_id="8" * 32, now=NOW)
    finally:
        web.close()
        collector.close()


@pytest.mark.parametrize("function", ["service_principal_guard()", "mock_task_decision_guard()", "request_task_once_guard()"])
def test_no_process_role_may_call_0013_s_security_definer_guards(pg_db, function):
    """Each runs as its owner: only its trigger calls it (REVOKE ALL ... FROM PUBLIC, as every other guard)."""
    with conn_as(pg_db, "portal_owner") as c:
        for role in ("portal_web", "portal_writer", "portal_collector"):
            assert c.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, function)).fetchone()[0] is False, role


def test_the_collector_creates_no_principal_for_a_decommissioned_app(pg_db):
    """Final review I1: service_principal_guard checks the app's lifecycle state, as the request's insert guard does."""
    from portal.store.base import ACTION_STEPS
    from portal.store.postgres import PostgresStore
    pid, admin = person(pg_db, "owner"), person(pg_db, "admin")
    with conn_as(pg_db, "portal_web") as c:
        request_id = sp(c, pid)
    owner = PostgresStore(pg_db("portal_owner"))
    try:
        life = owner.create_lifecycle(request_id=rid(), person_id=admin, repo="acme/notes", final={**LIFE, "action": "decommission"},
                                      note=LIFE_NOTE, now=NOW)
        assert owner.claim_approved(now=NOW).id == life.id
        owner.add_lifecycle_steps(request_id=life.id, steps=ACTION_STEPS["decommission"], now=NOW)
        owner.move_lifecycle_step(request_id=life.id, step="deploy", to_state="done", now=NOW, merge_sha="3" * 40)
        owner.set_lifecycle_state(request_id=life.id, state="decommissioned", now=NOW)
    finally:
        owner.close()
    with conn_as(pg_db, "portal_collector") as c:
        with pytest.raises(errors.RaiseException, match="is decommissioned"):
            c.execute("INSERT INTO service_principal (id, repo, display_name, client_id, scopes, request_id, created_at) "
                      "VALUES (%s, 'acme/notes', 'sp-notes-calendar', %s, ARRAY['Calendars.Read'], %s, now())", (SP_ID, CLIENT_ID, request_id))


def test_0013_applies_to_a_populated_database_migrated_through_0012(pg_server, monkeypatch):
    """Forward only, safe on existing rows: a registration, a binding and a lifecycle request survive with the new columns' defaults; the
    writer still claims the binding; apply is then idempotent; a service principal request is taken."""
    from portal import migrate
    from portal.store.postgres import PostgresStore
    from test_portal_store_contract import binding_final
    every = migrate.migrations()
    name = f"portal_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server.dsn(), autocommit=True) as conn:
        conn.execute(sql.Composed([sql.SQL("CREATE DATABASE "), sql.Identifier(name), sql.SQL(" OWNER portal_owner")]))
    dsn = lambda role="portal_owner": pg_server.dsn(role, name)  # noqa: E731
    try:
        monkeypatch.setattr(migrate, "migrations", lambda: [p for p in every if p.stem[:4] <= "0012"])
        assert migrate.apply(dsn())[-1] == "0012_lifecycle"
        store, web, writer = PostgresStore(dsn()), PostgresStore(dsn("portal_web")), PostgresStore(dsn("portal_writer"))
        try:
            owner, _, _ = web.sign_in(issuer="i", subject="o", display_name="O", email="o@example.test", groups=[], roles=[], now=NOW,
                                      max_age=dt.timedelta(hours=8))
            reg = web.create_registration(person_id=owner.id, now=NOW, ownership_acknowledged_at=NOW, draft={
                "schema": "registration-draft/v1", "repo": "acme/notes", "purpose": "Stores short team notes.", "data_classification": "public"})
            bid = "r_" + "b" * 32
            web.create_binding(request_id=bid, person_id=owner.id, final=binding_final("acme/notes", owner.id, bid), now=NOW)
            monkeypatch.setattr(migrate, "migrations", lambda: every)
            assert migrate.apply(dsn()) == ["0013_service_principals"] and migrate.apply(dsn()) == []
            assert store.get_request(reg.id).state == "pending" and store.service_principal_requests() == []
            assert writer.claim_approved(now=NOW).id == bid
            made = web.create_service_principal_request(request_id="r_" + "d" * 32, person_id=owner.id, repo="acme/notes", draft=SP_APPROVED,
                                                        state="approved", now=NOW)
            assert (made.kind, made.state) == ("service_principal", "approved")
            web.create_registration(person_id=owner.id, now=NOW, draft={"schema": "registration-draft/v1", "repo": "acme/tools",
                                                                        "purpose": "Builds internal tools.", "data_classification": "public"})
        finally:
            for s in (store, web, writer):
                s.close()
    finally:
        with psycopg.connect(pg_server.dsn(), autocommit=True) as conn:
            conn.execute(sql.Composed([sql.SQL("DROP DATABASE "), sql.Identifier(name), sql.SQL(" WITH (FORCE)")]))


def test_the_writer_s_role_builds_the_overlay_and_writes_a_binding_to_a_created_principal(pg_db):
    """S8 on Postgres: portal_web and portal_writer read service_principal (SELECT only, 0013), so both build the same overlay; a
    binding to the principal the collector created for acme/notes passes next_binding in the web and the writer's recheck."""
    from portal.binding_record import BindingRefused, next_binding
    from portal.directory import overlay
    from portal.ids import new_id
    from portal.store.base import RepoAdmin
    from portal.store.postgres import PostgresStore
    from portal.writer.run import run_once
    from portal_fakes import fake_directory
    from test_portal_writer_run import bcfg, bgh_for_write, client

    owner, admin = person(pg_db, "owner"), person(pg_db, "admin")
    web, writer, collector = (PostgresStore(pg_db(role)) for role in ("portal_web", "portal_writer", "portal_collector"))
    try:
        web.add_link(person_id=owner, github_login="owner-gh", github_user_id=1001, source="admin", actor_id=admin,
                     reason="seeded for the overlay test", now=NOW)
        collector.replace_repo_admins([RepoAdmin("acme/notes", "owner-gh", 1001, NOW)], observed_at=NOW)
        made = [collector.create_service_principal(request_id=web.create_service_principal_request(
                    request_id=rid(), person_id=owner, repo=repo, draft={**SP_APPROVED, "repo": repo, "display_name": name},
                    state="approved", now=NOW).id, principal_id=pid, client_id=cid, now=NOW)
                for repo, name, pid, cid in (("acme/notes", "sp-notes-calendar", SP_ID, CLIENT_ID),
                                             ("acme/tools", "sp-tools-x", "sp-" + "b" * 24, "1e2f6c1d-3b4a-4c5d-8e6f-7a8b9c0d1e2f"))]
        base = fake_directory()
        seen_web, seen_writer = overlay(base, web, "acme"), overlay(base, writer, "acme")
        assert seen_web == seen_writer and seen_writer.principal(SP_ID).client_id == CLIENT_ID
        assert [p.id for p in seen_writer.principals_for("acme", "acme/notes") if p.id in (made[0].id, made[1].id)] == [SP_ID]
        with pytest.raises(BindingRefused):   # acme/tools' principal is never offered for acme/notes
            next_binding(current=None, repo="acme/notes", org="acme", directory=seen_web, person_id=owner, request_id=rid(), now=NOW,
                         environment="test", principal_id=made[1].id)
        request_id = new_id("r")
        doc = next_binding(current=None, repo="acme/notes", org="acme", directory=seen_web, person_id=owner, request_id=request_id,
                           now=NOW, environment="test", principal_id=SP_ID)
        web.create_binding(request_id=request_id, person_id=owner, final=doc, now=NOW)
        run_once(writer, client(bgh_for_write()), bcfg(), NOW)
        assert web.get_request(request_id).state == "pr_open", web.get_request(request_id).failure_reason
    finally:
        for s in (web, writer, collector):
            s.close()
