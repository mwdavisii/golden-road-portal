"""Migration 0012 (spec 2026-11-02): the lifecycle kind and its record, rule 4 after a decommission, the request's own moves, the steps
only the writer writes, the commands the web asks for, the outbox whose task is the request's own (a SECURITY DEFINER trigger), the
grants, the migration on a populated database, and each process role's work through PostgresStore."""

import datetime as dt
import re
import textwrap
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.types.json import Jsonb
from portal_fakes import evidence_doc
from portal_pg import pg_db, pg_server  # noqa: F401

from portal.store.base import ACTION_STEPS

pytestmark = pytest.mark.docker
NOW = dt.datetime(2026, 11, 2, 12, tzinfo=dt.UTC)
FINAL = {"action": "suspend", "task": "SCTASK0010001", "task_sys_id": "e" * 32, "reason": "Critical vulnerability; suspended until patched"}
DRAFT = {"schema": "registration-draft/v1", "repo": "acme/notes", "purpose": "Stores short team notes.", "data_classification": "public"}
INSERT = ("INSERT INTO request (id, kind, repo, state, draft, final, requested_by, created_at, updated_at) "
          "VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now())")


def conn_as(pg_db, role):
    return psycopg.connect(pg_db(role), autocommit=True)


def person(pg_db, subject, email=None):
    from portal.store.postgres import PostgresStore
    store = PostgresStore(pg_db("portal_web"))
    try:
        found, _, _ = store.sign_in(issuer="i", subject=subject, display_name=subject, email=email, groups=[], roles=[], now=NOW,
                                    max_age=dt.timedelta(hours=8))
        return found.id
    finally:
        store.close()


def rid(n):
    return "r_" + f"{n:x}" * 32 if n < 16 else "r_" + uuid.uuid4().hex


def lifecycle(c, request_id, pid, repo="acme/notes", final=None, draft=None, state="approved"):
    final = FINAL if final is None else final
    c.execute(INSERT, (request_id, "lifecycle", repo, state, Jsonb(final if draft is None else draft), Jsonb(final), pid))


def writing(pg_db, request_id):
    with conn_as(pg_db, "portal_writer") as c:
        c.execute("UPDATE request SET state = 'writing', updated_at = now() WHERE id = %s", (request_id,))


def registration(c, pid, repo="acme/notes"):
    c.execute("INSERT INTO request (id, kind, repo, state, draft, requested_by, created_at, updated_at) "
              "VALUES (%s, 'registration', %s, 'pending', %s, %s, now(), now())", ("r_" + uuid.uuid4().hex, repo, Jsonb({**DRAFT, "repo": repo}), pid))


def binding(c, pid, repo="acme/notes"):
    from test_portal_store_contract import binding_final
    request_id = "r_" + uuid.uuid4().hex
    final = Jsonb(binding_final(repo, pid, request_id))
    c.execute(INSERT, (request_id, "binding", repo, "approved", final, final, pid))


def decommission(pg_db, pid, repo="acme/notes"):
    """A decommission request the writer claimed, and the state its deploy step set (as the writer)."""
    request_id = "r_" + uuid.uuid4().hex
    with conn_as(pg_db, "portal_web") as c:
        lifecycle(c, request_id, pid, repo, {**FINAL, "action": "decommission"})
    writing(pg_db, request_id)
    with conn_as(pg_db, "portal_writer") as c:
        c.execute("INSERT INTO lifecycle_state (repo, state, task, since, request) VALUES (%s, 'decommissioned', 'SCTASK0010001', now(), %s)",
                  (repo, request_id))
    return request_id


# ---- the kind and the record ----

@pytest.mark.parametrize("change", [
    {"state": "pending"},
    {"draft": {**FINAL, "reason": "Another reason, not the final's"}},
    {"final": {**FINAL, "extra": 1}},
    {"final": {**FINAL, "task": "SCTASK1"}},
    {"final": {**FINAL, "task_sys_id": "E" * 32}},
    {"final": {**FINAL, "action": "pause"}},
    {"final": {**FINAL, "reason": "two\nlines of reason"}},
    {"final": {**FINAL, "reason": "too short"}},
    {"final": {k: v for k, v in FINAL.items() if k != "reason"}},
])
def test_a_lifecycle_request_starts_approved_and_its_record_is_exactly_its_draft(pg_db, change):
    pid = person(pg_db, "admin")
    with conn_as(pg_db, "portal_web") as c:
        with pytest.raises(errors.RaiseException):
            lifecycle(c, rid(1), pid, **change)
        lifecycle(c, rid(1), pid)
        assert c.execute("SELECT kind, state FROM request WHERE id = %s", (rid(1),)).fetchone() == ("lifecycle", "approved")


# ---- rule 4 (decided while planning: items 15 and 20) ----

@pytest.mark.parametrize("insert", [registration, binding, lambda c, pid, repo: lifecycle(c, "r_" + uuid.uuid4().hex, pid, repo)])
def test_after_decommission_no_request_is_taken_for_the_repo_in_any_case(pg_db, insert):
    pid = person(pg_db, "admin")
    decommission(pg_db, pid)
    with conn_as(pg_db, "portal_web") as c:
        with pytest.raises(errors.RaiseException) as err:
            insert(c, pid, "ACME/Notes")
        assert "is decommissioned" in str(err.value)
        insert(c, pid, "acme/other")


def test_decommissioned_is_for_good_but_the_owner_may_bring_the_app_back_by_hand(pg_db):
    pid = person(pg_db, "admin")
    decommission(pg_db, pid)
    with conn_as(pg_db, "portal_writer") as c:
        for statement in ("UPDATE lifecycle_state SET state = 'active', task = NULL",
                          "UPDATE lifecycle_state SET state = 'suspended', task = 'SCTASK0010001'"):
            with pytest.raises(errors.RaiseException) as err:
                c.execute(statement)
            assert "for good" in str(err.value)
    for role in ("portal_writer", "portal_owner"):
        with conn_as(pg_db, role) as c:
            for statement in ("DELETE FROM lifecycle_state", "TRUNCATE lifecycle_state CASCADE"):
                with pytest.raises((errors.RaiseException, errors.InsufficientPrivilege)):
                    c.execute(statement)
    with conn_as(pg_db, "portal_owner") as c:
        with pytest.raises(errors.RaiseException):
            c.execute("UPDATE lifecycle_state SET state = 'suspended', task = 'SCTASK0010001'")  # only back to active
        with pytest.raises(errors.RaiseException, match="applied in git or stopped"):  # I-1: the decommission is still writing
            c.execute("UPDATE lifecycle_state SET state = 'active', task = NULL")
    with conn_as(pg_db, "portal_writer") as c:
        c.execute("UPDATE request SET state = 'failed', failure_reason = 'stopped', updated_at = now() WHERE kind = 'lifecycle'")
    with conn_as(pg_db, "portal_owner") as c:
        c.execute("UPDATE lifecycle_state SET state = 'active', task = NULL")  # procedure 18, item 20
        assert c.execute("SELECT count(*) FROM lifecycle_state WHERE state = 'active'").fetchone()[0] == 1
    with conn_as(pg_db, "portal_web") as c:
        registration(c, pid, "ACME/Notes")


STATE = "INSERT INTO lifecycle_state (repo, state, task, since, request) VALUES (%s, %s, %s, now(), %s)"


def test_a_lifecycle_state_is_set_by_a_lifecycle_request_with_the_database_s_time(pg_db):
    pid = person(pg_db, "admin")
    with conn_as(pg_db, "portal_web") as c:
        lifecycle(c, rid(1), pid)
        registration(c, pid, "acme/tools")
        other = c.execute("SELECT id FROM request WHERE kind = 'registration'").fetchone()[0]
    writing(pg_db, rid(1))
    with conn_as(pg_db, "portal_writer") as c:
        with pytest.raises(errors.RaiseException) as err:
            c.execute(STATE, ("acme/tools", "suspended", "SCTASK0010001", other))
        assert "names the lifecycle request" in str(err.value)
        c.execute("INSERT INTO lifecycle_state (repo, state, task, since, request) VALUES ('acme/notes', 'suspended', 'SCTASK0010001', "
                  "'2001-01-01', %s)", (rid(1),))
        assert c.execute("SELECT since FROM lifecycle_state").fetchone()[0] > NOW - dt.timedelta(days=3650)
        with pytest.raises(errors.UniqueViolation):
            c.execute(STATE, ("ACME/Notes", "suspended", "SCTASK0010001", rid(1)))
        with pytest.raises(errors.RaiseException):
            c.execute("UPDATE lifecycle_state SET repo = 'acme/other'")


@pytest.mark.parametrize("repo, state, task", [
    ("acme/other", "suspended", "SCTASK0010001"),  # another repo than the request's
    ("acme/notes", "active", None),  # another state than a suspend applies
    ("acme/notes", "decommissioned", "SCTASK0010001"),
    ("acme/notes", "suspended", "SCTASK0010002"),  # another task than the request's
])
def test_a_lifecycle_state_is_the_one_its_request_applies(pg_db, repo, state, task):
    pid = person(pg_db, "admin")
    with conn_as(pg_db, "portal_web") as c:
        lifecycle(c, rid(1), pid)
    writing(pg_db, rid(1))
    with conn_as(pg_db, "portal_writer") as c:
        with pytest.raises(errors.RaiseException) as err:
            c.execute(STATE, (repo, state, task, rid(1)))
        assert "the one its request applies" in str(err.value)
        c.execute(STATE, ("Acme/Notes", "suspended", "SCTASK0010001", rid(1)))  # the repo in any case
    with conn_as(pg_db, "portal_owner") as c:
        with pytest.raises(errors.RaiseException, match="the one its request applies"):  # the owner too, but for procedure 18
            c.execute("UPDATE lifecycle_state SET task = NULL")


def test_a_lifecycle_state_is_set_only_while_its_request_is_being_written(pg_db):
    pid = person(pg_db, "admin")
    with conn_as(pg_db, "portal_web") as c:
        lifecycle(c, rid(1), pid)
    with conn_as(pg_db, "portal_writer") as c:
        with pytest.raises(errors.RaiseException) as err:  # approved: not claimed yet
            c.execute(STATE, ("acme/notes", "suspended", "SCTASK0010001", rid(1)))
        assert "while its request is being written" in str(err.value)


# ---- the request's moves ----

@pytest.mark.parametrize("merge_sha", [None, "3" * 40])
def test_the_writer_applies_a_lifecycle_request_without_a_pull_request_of_its_own(pg_db, merge_sha):
    pid = person(pg_db, "admin")
    with conn_as(pg_db, "portal_web") as c:
        lifecycle(c, rid(1), pid)
    writing(pg_db, rid(1))
    apply = "UPDATE request SET state = 'merged', merge_sha = %s WHERE id = %s"
    with conn_as(pg_db, "portal_writer") as c:
        with pytest.raises(errors.RaiseException) as err:
            c.execute("UPDATE request SET state = 'pr_open', branch = 'b', pr_number = 7, pr_url = 'u', head_sha = %s, base_sha = %s "
                      "WHERE id = %s", ("2" * 40, "1" * 40, rid(1)))
        assert "keeps its pull requests on its steps" in str(err.value)
        c.execute("INSERT INTO lifecycle_step (request_id, step, position, state, updated_at) VALUES (%s, 'deploy', 1, 'planned', now())",
                  (rid(1),))
        for before in ("", "UPDATE lifecycle_step SET state = 'done' WHERE request_id = %s"):  # no step done; no state set
            if before:
                c.execute(before, (rid(1),))
            with pytest.raises(errors.RaiseException) as err:
                c.execute(apply, (merge_sha, rid(1)))
            assert "every one of its steps is done" in str(err.value)
        c.execute(STATE, ("acme/notes", "suspended", "SCTASK0010001", rid(1)))
        c.execute(apply, (merge_sha, rid(1)))
        assert c.execute("SELECT state, merge_sha FROM request WHERE id = %s", (rid(1),)).fetchone() == ("merged", merge_sha)


def test_only_a_lifecycle_request_goes_from_writing_to_merged(pg_db):
    pid = person(pg_db, "admin")
    with conn_as(pg_db, "portal_web") as c:
        binding(c, pid)
        bid = c.execute("SELECT id FROM request WHERE kind = 'binding'").fetchone()[0]
    writing(pg_db, bid)
    with conn_as(pg_db, "portal_writer") as c:
        with pytest.raises(errors.RaiseException) as err:
            c.execute("UPDATE request SET state = 'merged', merge_sha = %s WHERE id = %s", ("3" * 40, bid))
        assert "only a lifecycle request" in str(err.value)


# ---- steps ----

def steps(pg_db, pid, action="decommission"):
    with conn_as(pg_db, "portal_web") as c:
        lifecycle(c, rid(1), pid, final={**FINAL, "action": action})
    writing(pg_db, rid(1))
    with conn_as(pg_db, "portal_writer") as c:
        for i, name in enumerate(ACTION_STEPS[action]):
            c.execute("INSERT INTO lifecycle_step (request_id, step, position, state, updated_at) VALUES (%s, %s, %s, 'planned', now())",
                      (rid(1), name, i + 1))


def test_only_the_writer_writes_a_step(pg_db):
    pid = person(pg_db, "admin")
    steps(pg_db, pid)
    with conn_as(pg_db, "portal_web") as c:
        assert c.execute("SELECT count(*) FROM lifecycle_step").fetchone()[0] == 3
        for statement in ("INSERT INTO lifecycle_step (request_id, step, position, state, updated_at) VALUES (%s, 'x', 1, 'planned', now())",
                          "UPDATE lifecycle_step SET state = 'done' WHERE request_id = %s"):
            with pytest.raises(errors.InsufficientPrivilege):
                c.execute(statement, (rid(1),))
    with conn_as(pg_db, "portal_collector") as c:
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("SELECT 1 FROM lifecycle_step")
    for role in ("portal_writer", "portal_owner"):
        with conn_as(pg_db, role) as c:
            for statement in ("DELETE FROM lifecycle_step", "TRUNCATE lifecycle_step"):
                with pytest.raises((errors.RaiseException, errors.InsufficientPrivilege)):
                    c.execute(statement)


def test_a_step_starts_planned_and_belongs_to_a_lifecycle_request(pg_db):
    pid = person(pg_db, "admin")
    steps(pg_db, pid, action="suspend")
    with conn_as(pg_db, "portal_web") as c:
        binding(c, pid, "acme/tools")
        bid = c.execute("SELECT id FROM request WHERE kind = 'binding'").fetchone()[0]
    with conn_as(pg_db, "portal_writer") as c:
        with pytest.raises(errors.RaiseException) as err:
            c.execute("INSERT INTO lifecycle_step (request_id, step, position, state, updated_at) VALUES (%s, 'deploy', 1, 'planned', now())",
                      (bid,))
        assert "belongs to a lifecycle request" in str(err.value)
        with pytest.raises(errors.RaiseException):
            c.execute("INSERT INTO lifecycle_step (request_id, step, position, state, updated_at, merge_sha) "
                      "VALUES (%s, 'factory', 2, 'done', now(), %s)", (rid(1), "3" * 40))
        with pytest.raises(errors.UniqueViolation):
            c.execute("INSERT INTO lifecycle_step (request_id, step, position, state, updated_at) VALUES (%s, 'deploy', 2, 'planned', now())",
                      (rid(1),))


def test_a_step_moves_only_by_its_transitions_and_its_time_is_the_database_s(pg_db):
    pid = person(pg_db, "admin")
    steps(pg_db, pid)
    move = "UPDATE lifecycle_step SET state = %s, attempt = %s, failure_reason = %s, merge_sha = %s WHERE request_id = %s AND step = %s"
    with conn_as(pg_db, "portal_writer") as c:
        with pytest.raises(errors.RaiseException):  # every UPDATE is one of the moves: planned -> planned is none
            c.execute("UPDATE lifecycle_step SET updated_at = now() WHERE request_id = %s AND step = 'deploy'", (rid(1),))
        c.execute("UPDATE lifecycle_step SET state = 'done', merge_sha = %s, updated_at = '2001-01-01' WHERE request_id = %s AND step = 'deploy'",
                  ("3" * 40, rid(1)))
        assert c.execute("SELECT updated_at FROM lifecycle_step WHERE step = 'deploy'").fetchone()[0] > NOW - dt.timedelta(days=3650)
        with pytest.raises(errors.RaiseException) as err:
            c.execute(move, ("planned", 2, None, None, rid(1), "deploy"))
        assert "illegal lifecycle step transition" in str(err.value)
        c.execute(move, ("failed", 1, "archive: github: 404", None, rid(1), "archive"))
        with pytest.raises(errors.RaiseException) as err:
            c.execute(move, ("planned", 1, None, None, rid(1), "archive"))
        assert "new attempt" in str(err.value)
        c.execute(move, ("planned", 2, None, None, rid(1), "archive"))
        with pytest.raises(errors.RaiseException):
            c.execute("UPDATE lifecycle_step SET step = 'factory', position = 3 WHERE request_id = %s AND step = 'archive'", (rid(1),))
        with pytest.raises(errors.CheckViolation):  # an open step names all of its pull request
            c.execute("UPDATE lifecycle_step SET state = 'pr_open', branch = 'b' WHERE request_id = %s AND step = 'archive'", (rid(1),))
        c.execute(move, ("closed", 2, None, None, rid(1), "archive"))
        with pytest.raises(errors.RaiseException):
            c.execute(move, ("planned", 3, None, None, rid(1), "archive"))


# ---- commands ----

def test_the_web_asks_for_a_command_and_the_writer_marks_it_done_once(pg_db):
    pid = person(pg_db, "admin")
    with conn_as(pg_db, "portal_web") as c:
        lifecycle(c, rid(1), pid)
        c.execute("INSERT INTO lifecycle_command (request_id, command, requested_by) VALUES (%s, 'retry', %s)", (rid(1), pid))
        at = c.execute("SELECT requested_at FROM lifecycle_command").fetchone()[0]
        assert at > NOW - dt.timedelta(days=3650)
        with pytest.raises(errors.UniqueViolation):
            c.execute("INSERT INTO lifecycle_command (request_id, command, requested_by) VALUES (%s, 'abandon', %s)", (rid(1), pid))
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("UPDATE lifecycle_command SET done_at = now()")
        with pytest.raises(errors.InsufficientPrivilege):  # requested_at is the database's
            c.execute("INSERT INTO lifecycle_command (request_id, command, requested_by, requested_at) VALUES (%s, 'abandon', %s, now())",
                      (rid(1), pid))
        registration(c, pid, "acme/tools")
        other = c.execute("SELECT id FROM request WHERE kind = 'registration'").fetchone()[0]
        with pytest.raises(errors.RaiseException) as err:
            c.execute("INSERT INTO lifecycle_command (request_id, command, requested_by) VALUES (%s, 'retry', %s)", (other, pid))
        assert "is for a lifecycle request" in str(err.value)
    with conn_as(pg_db, "portal_writer") as c:
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("INSERT INTO lifecycle_command (request_id, command, requested_by) VALUES (%s, 'retry', %s)", (rid(1), pid))
        c.execute("UPDATE lifecycle_command SET done_at = '2001-01-01'")
        assert c.execute("SELECT done_at FROM lifecycle_command").fetchone()[0] > NOW - dt.timedelta(days=3650)
        with pytest.raises(errors.RaiseException) as err:
            c.execute("UPDATE lifecycle_command SET done_at = now()")
        assert "only marked done" in str(err.value)
    with conn_as(pg_db, "portal_web") as c:
        c.execute("INSERT INTO lifecycle_command (request_id, command, requested_by) VALUES (%s, 'abandon', %s)", (rid(1), pid))
    with conn_as(pg_db, "portal_owner") as c:
        for statement in ("DELETE FROM lifecycle_command", "TRUNCATE lifecycle_command"):
            with pytest.raises(errors.RaiseException):
                c.execute(statement)


# ---- events (the outbox) ----

def test_every_process_role_adds_a_note_and_its_task_is_the_request_s_own(pg_db):
    pid = person(pg_db, "admin")
    with conn_as(pg_db, "portal_web") as c:
        lifecycle(c, rid(1), pid)
    for role, step in (("portal_web", "requested"), ("portal_writer", "pr_opened"), ("portal_collector", "ci_corrected")):
        with conn_as(pg_db, role) as c:
            c.execute("INSERT INTO lifecycle_events (request_id, step, note) VALUES (%s, %s, 'A note.')", (rid(1), step))
            with pytest.raises(errors.InsufficientPrivilege):  # the task, the time and the counts are the database's
                c.execute("INSERT INTO lifecycle_events (request_id, step, note, task_sys_id) VALUES (%s, %s, 'A note.', %s)",
                          (rid(1), step, "f" * 32))
    with conn_as(pg_db, "portal_owner") as c:  # even the owner can't name another task
        c.execute("INSERT INTO lifecycle_events (request_id, step, note, task_sys_id, created_at, sent_at, attempts, last_error) "
                  "VALUES (%s, 'merged', 'A note.', %s, '2001-01-01', now(), 7, 'x')", (rid(1), "f" * 32))
        rows = c.execute("SELECT task_sys_id, created_at, sent_at, attempts, last_error FROM lifecycle_events ORDER BY id").fetchall()
    assert len(rows) == 4 and all(r[0] == "e" * 32 and r[1] > NOW - dt.timedelta(days=3650) and r[2:] == (None, 0, None) for r in rows)
    with conn_as(pg_db, "portal_web") as c:
        binding(c, pid, "acme/tools")
        bid = c.execute("SELECT id FROM request WHERE kind = 'binding'").fetchone()[0]
    with conn_as(pg_db, "portal_collector") as c:
        with pytest.raises(errors.RaiseException) as err:
            c.execute("INSERT INTO lifecycle_events (request_id, step, note) VALUES (%s, 'requested', 'A note.')", (bid,))
        assert "is for a lifecycle request" in str(err.value)


def test_only_the_collector_sends_a_note_once_and_counts_its_failures_one_by_one(pg_db):
    pid = person(pg_db, "admin")
    with conn_as(pg_db, "portal_web") as c:
        lifecycle(c, rid(1), pid)
        c.execute("INSERT INTO lifecycle_events (request_id, step, note) VALUES (%s, 'requested', 'A note.')", (rid(1),))
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("UPDATE lifecycle_events SET sent_at = now()")
    with conn_as(pg_db, "portal_writer") as c:
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("UPDATE lifecycle_events SET sent_at = now()")
    with conn_as(pg_db, "portal_collector") as c:
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("UPDATE lifecycle_events SET note = 'Another note.'")
        for bad in ("UPDATE lifecycle_events SET attempts = attempts + 2, last_error = 'x'", "UPDATE lifecycle_events SET attempts = attempts + 1",
                    "UPDATE lifecycle_events SET attempts = 0, last_error = 'x'"):
            with pytest.raises(errors.RaiseException) as err:
                c.execute(bad)
            assert "one more attempt" in str(err.value)
        c.execute("UPDATE lifecycle_events SET attempts = attempts + 1, last_error = 'ServiceNow answered 503'")
        c.execute("UPDATE lifecycle_events SET sent_at = '2001-01-01', attempts = 9, last_error = NULL")
        sent_at, attempts, error = c.execute("SELECT sent_at, attempts, last_error FROM lifecycle_events").fetchone()
        assert sent_at > NOW - dt.timedelta(days=3650) and (attempts, error) == (1, "ServiceNow answered 503")
        for statement in ("UPDATE lifecycle_events SET sent_at = now()", "UPDATE lifecycle_events SET attempts = attempts + 1, last_error = 'x'"):
            with pytest.raises(errors.RaiseException) as err:
                c.execute(statement)
            assert "sent once" in str(err.value)
    with conn_as(pg_db, "portal_owner") as c:
        with pytest.raises(errors.RaiseException):
            c.execute("UPDATE lifecycle_events SET note = 'Another note.'")
        for statement in ("DELETE FROM lifecycle_events", "TRUNCATE lifecycle_events"):
            with pytest.raises(errors.RaiseException):
                c.execute(statement)


# ---- grants ----

def test_the_collector_can_t_read_the_request_s_record_and_the_web_can_t_set_a_state(pg_db):
    pid = person(pg_db, "admin")
    with conn_as(pg_db, "portal_web") as c:
        lifecycle(c, rid(1), pid)
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("INSERT INTO lifecycle_state (repo, state, task, since, request) VALUES ('acme/notes', 'suspended', 'SCTASK0010001', "
                      "now(), %s)", (rid(1),))
    with conn_as(pg_db, "portal_collector") as c:
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("SELECT final FROM request")
        c.execute("INSERT INTO lifecycle_events (request_id, step, note) VALUES (%s, 'ci_corrected', 'A note.')", (rid(1),))
        assert c.execute("SELECT task_sys_id FROM lifecycle_events").fetchone()[0] == "e" * 32
        assert c.execute("SELECT count(*) FROM lifecycle_state").fetchone()[0] == 0
        for statement in ("SELECT 1 FROM lifecycle_step", "SELECT 1 FROM lifecycle_command"):
            with pytest.raises(errors.InsufficientPrivilege):
                c.execute(statement)
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("INSERT INTO lifecycle_state (repo, state, task, since, request) VALUES ('acme/notes', 'suspended', 'SCTASK0010001', "
                      "now(), %s)", (rid(1),))


# ---- the migration ----

def test_0012_applies_to_a_populated_database_migrated_through_0011(pg_server, monkeypatch):
    """Forward only, safe on existing rows: a registration, a binding, a CMDB record and observed rows survive; apply is then idempotent;
    a registration and a binding insert as before, and a lifecycle request is taken."""
    from portal import migrate
    from portal.store.base import IntakeObserved
    from portal.store.postgres import PostgresStore
    from test_portal_store_contract import binding_final
    every = migrate.migrations()
    name = f"portal_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server.dsn(), autocommit=True) as conn:
        conn.execute(sql.Composed([sql.SQL("CREATE DATABASE "), sql.Identifier(name), sql.SQL(" OWNER portal_owner")]))
    dsn = lambda role="portal_owner": pg_server.dsn(role, name)  # noqa: E731
    try:
        monkeypatch.setattr(migrate, "migrations", lambda: [p for p in every if p.stem[:4] <= "0011"])
        assert migrate.apply(dsn())[-1] == "0011_mcp_skills_and_cmdb"
        store, web, collector = PostgresStore(dsn()), PostgresStore(dsn("portal_web")), PostgresStore(dsn("portal_collector"))
        try:
            owner, _, _ = web.sign_in(issuer="i", subject="o", display_name="O", email="o@example.test", groups=[], roles=[], now=NOW,
                                      max_age=dt.timedelta(hours=8))
            reg = web.create_registration(person_id=owner.id, draft=DRAFT, now=NOW, ownership_acknowledged_at=NOW)
            store.add_evidence(request_id=reg.id, submission=1, commit="e" * 40, evidence=evidence_doc("acme/notes"), floor_tier="low",
                               floor_classification="public", rules_commit="f" * 40, now=NOW)
            bid = "r_" + "b" * 32
            web.create_binding(request_id=bid, person_id=owner.id, final=binding_final("acme/notes", owner.id, bid), now=NOW)
            collector.record_cmdb_sync(repo="acme/notes", sys_id="c" * 32, number="APM0001001", fields_hash="f" * 64, operation="INSERT",
                                       now=NOW)
            store.replace_intake_observed([IntakeObserved("acme/notes", "c" * 40, "intake/v2", True, owner.id, "low", "public",
                                                          "Stores notes.", dt.date(2027, 10, 1), None, None)], observed_at=NOW)
            monkeypatch.setattr(migrate, "migrations", lambda: every)
            assert migrate.apply(dsn()) == ["0012_lifecycle"] and migrate.apply(dsn()) == []
            assert store.get_request(reg.id).state == "pending" and store.get_request(bid).state == "approved"
            assert store.evidence_for(reg.id, 1).commit == "e" * 40 and [r.repo for r in store.intake_observed()] == ["acme/notes"]
            assert store.cmdb_record("acme/notes").number == "APM0001001" and store.lifecycle_states() == []
            web.create_registration(person_id=owner.id, draft={**DRAFT, "repo": "acme/tools"}, now=NOW)
            other = "r_" + "c" * 32
            web.create_binding(request_id=other, person_id=owner.id, final=binding_final("acme/tools", owner.id, other), now=NOW)
            made = web.create_lifecycle(request_id="r_" + "d" * 32, person_id=owner.id, repo="acme/notes", final=FINAL, now=NOW,
                                        note="Requested in the golden road portal by O: suspend the app. Reason: " + FINAL["reason"])
            assert (made.kind, made.state) == ("lifecycle", "approved")
        finally:
            for s in (store, web, collector):
                s.close()
    finally:
        with psycopg.connect(pg_server.dsn(), autocommit=True) as conn:
            conn.execute(sql.Composed([sql.SQL("DROP DATABASE "), sql.Identifier(name), sql.SQL(" WITH (FORCE)")]))


# ---- through PostgresStore, each process role doing its own work ----

def test_each_process_role_does_its_lifecycle_work_through_the_store(pg_db):
    from portal.store.base import ACTION_STEPS, Conflict
    from portal.store.postgres import PostgresStore
    pid = person(pg_db, "admin")
    web, writer, collector = (PostgresStore(pg_db(role)) for role in ("portal_web", "portal_writer", "portal_collector"))
    try:
        req = web.create_lifecycle(request_id=rid(1), person_id=pid, repo="acme/notes", final=FINAL, now=NOW,
                                   note="Requested in the golden road portal by admin: suspend the app. Reason: " + FINAL["reason"])
        command = web.add_lifecycle_command(request_id=req.id, command="retry", actor_id=pid, now=NOW)
        assert writer.open_lifecycle_command(req.id) == command
        assert writer.claim_approved(now=NOW).id == req.id
        [step] = writer.add_lifecycle_steps(request_id=req.id, steps=ACTION_STEPS["suspend"], now=NOW)
        assert (step.step, step.position, step.state, step.attempt) == ("deploy", 1, "planned", 1)
        writer.move_lifecycle_step(request_id=req.id, step="deploy", to_state="pr_open", now=NOW, branch="portal/lifecycle-x-deploy-1",
                                   pr_number=7, pr_url="https://github.com/acme/golden-road-deploy/pull/7", head_sha="2" * 40, base_sha="1" * 40)
        state = writer.set_lifecycle_state(request_id=req.id, state="suspended", now=NOW)
        writer.move_lifecycle_step(request_id=req.id, step="deploy", to_state="done", now=NOW, merge_sha="3" * 40)
        assert (state.state, state.task, state.request) == ("suspended", "SCTASK0010001", req.id)
        assert web.lifecycle_state("ACME/notes") == state and collector.lifecycle_state("acme/notes") == state
        writer.finish_lifecycle_command(command_id=command.id, now=NOW)
        assert web.open_lifecycle_command(req.id) is None
        with pytest.raises(Conflict) as err:
            writer.finish_lifecycle_command(command_id=command.id, now=NOW)
        assert err.value.code == "wrong_state"
        writer.add_lifecycle_event(request_id=req.id, step="merged", note="Pull request #7 merged.", now=NOW)
        applied = writer.apply_lifecycle(request_id=req.id, merge_sha="3" * 40, note="Suspended: the task is closed.", now=NOW)
        assert (applied.state, applied.merge_sha, writer.lifecycle_state("acme/notes")) == ("merged", "3" * 40, state)
        first, second, third = collector.lifecycle_events(unsent=True)
        assert (first.step, second.step, third.step, first.task_sys_id) == ("requested", "merged", "completed", "e" * 32)
        collector.mark_lifecycle_event_failed(event_id=first.id, error="ServiceNow answered 503 for the task's work note", now=NOW)
        for event in (first, second, third):
            collector.mark_lifecycle_event_sent(event_id=event.id, now=NOW)
        corrected = collector.add_lifecycle_event(request_id=req.id, step="ci_corrected", note="The CI's lifecycle fields were set back.",
                                                  now=NOW)
        assert [e.id for e in collector.lifecycle_events(unsent=True)] == [corrected.id]
        assert [(e.step, e.attempts) for e in web.lifecycle_events(req.id)] == [("requested", 1), ("merged", 0), ("completed", 0),
                                                                               ("ci_corrected", 0)]
        with pytest.raises(errors.InsufficientPrivilege):
            collector.set_lifecycle_state(request_id=req.id, state="suspended", now=NOW)
        with pytest.raises(errors.InsufficientPrivilege):
            collector.apply_lifecycle(request_id=req.id, merge_sha=None, note="Done.", now=NOW)
    finally:
        for s in (web, writer, collector):
            s.close()


# ---- pinned search paths: a temp table never shadows a table a guard reads ----

def test_a_temp_table_never_shadows_the_lifecycle_state_the_insert_guard_reads(pg_db):
    """pg_temp is searched first unless a function pins it last: as portal_web, a temp lifecycle_state with no rows would hide the
    decommission from request_insert_guard."""
    pid = person(pg_db, "admin")
    decommission(pg_db, pid)
    with conn_as(pg_db, "portal_web") as c:
        c.execute("CREATE TEMP TABLE lifecycle_state (repo text, state text)")
        with pytest.raises(errors.RaiseException) as err:
            registration(c, pid)
        assert "is decommissioned" in str(err.value)


def test_a_temp_table_never_shadows_the_evidence_the_floor_guard_reads(pg_db):
    """0005's approval floor guard, pinned by 0012: a temp registration_evidence is no evidence."""
    from test_portal_postgres_evidence_docker import COMMIT, decide_sql, final_for, seed
    ids = seed(pg_db)
    with conn_as(pg_db, "portal_web") as c:
        c.execute("CREATE TEMP TABLE registration_evidence (request_id text, submission integer, problem text, commit_sha text, "
                  "floor_tier text, floor_classification text, record jsonb)")
        c.execute("INSERT INTO registration_evidence VALUES (%s, 1, NULL, %s, 'low', 'public', NULL)", (ids["request"], COMMIT))
        with pytest.raises(errors.RaiseException) as err:
            c.execute(*decide_sql(ids, final_for()))
        assert "needs the platform's evidence" in str(err.value)


def test_every_trigger_function_pins_its_search_path_with_pg_temp_last(pg_db):
    with conn_as(pg_db, "portal_owner") as c:
        loose = c.execute("SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                          "WHERE n.nspname = 'public' AND p.prorettype = 'trigger'::regtype "
                          "AND NOT coalesce('search_path=public, pg_temp' = ANY (p.proconfig), false) ORDER BY 1").fetchall()
    assert loose == []


# ---- the outbox's steps by role, one requested note, and the send order ----

EVENT = "INSERT INTO lifecycle_events (request_id, step, note) VALUES (%s, %s, 'A note.')"


@pytest.mark.parametrize("role, refused, allowed", [
    ("portal_web", ("pr_opened", "completed", "ci_corrected"), "requested"),
    ("portal_collector", ("requested", "merged", "completed"), "ci_corrected"),
    ("portal_writer", ("requested", "ci_corrected"), "completed"),
])
def test_each_process_role_adds_only_its_own_steps(pg_db, role, refused, allowed):
    """session_user, not current_user: the guard runs as its owner (SECURITY DEFINER)."""
    pid = person(pg_db, "admin")
    with conn_as(pg_db, "portal_web") as c:
        lifecycle(c, rid(1), pid)
    with conn_as(pg_db, role) as c:
        for step in refused:
            with pytest.raises(errors.RaiseException) as err:
                c.execute(EVENT, (rid(1), step))
            assert "isn't this role's" in str(err.value)
        c.execute(EVENT, (rid(1), allowed))
        assert c.execute("SELECT step FROM lifecycle_events").fetchall() == [(allowed,)]


def test_a_request_has_one_requested_note_and_one_completed_note(pg_db):
    pid = person(pg_db, "admin")
    with conn_as(pg_db, "portal_web") as c:
        lifecycle(c, rid(1), pid)
        c.execute(EVENT, (rid(1), "requested"))
        with pytest.raises(errors.UniqueViolation):
            c.execute(EVENT, (rid(1), "requested"))
    with conn_as(pg_db, "portal_writer") as c:
        c.execute(EVENT, (rid(1), "completed"))
        with pytest.raises(errors.UniqueViolation):
            c.execute(EVENT, (rid(1), "completed"))


def test_the_web_s_store_refuses_a_note_that_isn_t_its_own(pg_db):
    from portal.store.base import Conflict
    from portal.store.postgres import PostgresStore
    pid = person(pg_db, "admin")
    web = PostgresStore(pg_db("portal_web"))
    try:
        req = web.create_lifecycle(request_id=rid(1), person_id=pid, repo="acme/notes", final=FINAL, now=NOW,
                                   note="Requested in the golden road portal by admin: suspend the app. Reason: " + FINAL["reason"])
        with pytest.raises(Conflict) as err:
            web.add_lifecycle_event(request_id=req.id, step="completed", note="Closed by the web.", now=NOW)
        assert err.value.code == "wrong_role"
    finally:
        web.close()


def test_the_collector_sends_a_request_s_notes_in_order(pg_db):
    pid = person(pg_db, "admin")
    with conn_as(pg_db, "portal_web") as c:
        lifecycle(c, rid(1), pid)
        c.execute(EVENT, (rid(1), "requested"))
    with conn_as(pg_db, "portal_writer") as c:
        c.execute(EVENT, (rid(1), "pr_opened"))
    with conn_as(pg_db, "portal_collector") as c:
        first, second = (r[0] for r in c.execute("SELECT id FROM lifecycle_events ORDER BY id").fetchall())
        with pytest.raises(errors.RaiseException) as err:
            c.execute("UPDATE lifecycle_events SET sent_at = now() WHERE id = %s", (second,))
        assert "after every earlier one" in str(err.value)
        c.execute("UPDATE lifecycle_events SET sent_at = now() WHERE id = %s", (first,))
        c.execute("UPDATE lifecycle_events SET sent_at = now() WHERE id = %s", (second,))


# ---- the steps' order and their pull requests ----

def test_a_step_leaves_planned_only_after_every_earlier_step_is_done(pg_db):
    """Rule 7: deploy, then archive, then factory (decided by the lead while Mike was away); an abandon closes any step."""
    pid = person(pg_db, "admin")
    steps(pg_db, pid)
    with conn_as(pg_db, "portal_writer") as c:
        for step in ("archive", "factory"):
            with pytest.raises(errors.RaiseException) as err:
                c.execute("UPDATE lifecycle_step SET state = 'done' WHERE request_id = %s AND step = %s", (rid(1), step))
            assert "leaves planned only after every earlier step is done" in str(err.value)
        c.execute("UPDATE lifecycle_step SET state = 'done' WHERE request_id = %s AND step = 'deploy'", (rid(1),))
        with pytest.raises(errors.RaiseException):
            c.execute("UPDATE lifecycle_step SET state = 'failed', failure_reason = 'x' WHERE request_id = %s AND step = 'factory'", (rid(1),))
        c.execute("UPDATE lifecycle_step SET state = 'closed' WHERE request_id = %s AND step = 'factory'", (rid(1),))
        c.execute("UPDATE lifecycle_step SET state = 'done' WHERE request_id = %s AND step = 'archive'", (rid(1),))


def test_a_step_moves_only_while_its_request_is_being_written(pg_db):
    pid = person(pg_db, "admin")
    steps(pg_db, pid, action="suspend")
    with conn_as(pg_db, "portal_writer") as c:
        c.execute("UPDATE lifecycle_step SET state = 'failed', failure_reason = 'x' WHERE request_id = %s", (rid(1),))
        c.execute("UPDATE request SET state = 'failed', failure_reason = 'abandoned' WHERE id = %s", (rid(1),))
        with pytest.raises(errors.RaiseException) as err:
            c.execute("UPDATE lifecycle_step SET state = 'planned', attempt = 2, failure_reason = NULL WHERE request_id = %s", (rid(1),))
        assert "while its request is being written" in str(err.value)
    with conn_as(pg_db, "portal_web") as c:
        lifecycle(c, rid(2), pid, repo="acme/tools")
    with conn_as(pg_db, "portal_writer") as c:
        with pytest.raises(errors.RaiseException):  # approved: not claimed, so not planned
            c.execute("INSERT INTO lifecycle_step (request_id, step, position, state, updated_at) VALUES (%s, 'deploy', 1, 'planned', now())",
                      (rid(2),))


# Each statement is a literal (Semgrep refuses composed SQL); the column is the only thing that differs.
CHANGE_PR = {
    "branch": "UPDATE lifecycle_step SET state = %s, failure_reason = %s, branch = %s WHERE request_id = %s",
    "pr_number": "UPDATE lifecycle_step SET state = %s, failure_reason = %s, pr_number = %s WHERE request_id = %s",
    "pr_url": "UPDATE lifecycle_step SET state = %s, failure_reason = %s, pr_url = %s WHERE request_id = %s",
    "head_sha": "UPDATE lifecycle_step SET state = %s, failure_reason = %s, head_sha = %s WHERE request_id = %s",
    "base_sha": "UPDATE lifecycle_step SET state = %s, failure_reason = %s, base_sha = %s WHERE request_id = %s",
}


@pytest.mark.parametrize("column, value", [("branch", "other"), ("pr_number", 8), ("pr_url", "https://github.com/x/y/pull/8"),
                                           ("head_sha", "4" * 40), ("base_sha", "5" * 40)])
@pytest.mark.parametrize("to_state, reason", [("done", None), ("failed", "x"), ("closed", None)])
def test_an_open_step_s_pull_request_never_changes(pg_db, column, value, to_state, reason):
    pid = person(pg_db, "admin")
    steps(pg_db, pid, action="suspend")
    with conn_as(pg_db, "portal_writer") as c:
        c.execute("UPDATE lifecycle_step SET state = 'pr_open', branch = 'b', pr_number = 7, pr_url = 'https://github.com/x/y/pull/7', "
                  "head_sha = %s, base_sha = %s WHERE request_id = %s", ("2" * 40, "1" * 40, rid(1)))
        with pytest.raises(errors.RaiseException) as err:
            c.execute(CHANGE_PR[column], (to_state, reason, value, rid(1)))
        assert "never changes" in str(err.value)
        c.execute("UPDATE lifecycle_step SET state = %s, failure_reason = %s WHERE request_id = %s", (to_state, reason, rid(1)))
        assert c.execute("SELECT branch, pr_number FROM lifecycle_step").fetchone() == ("b", 7)


PLANNED_WITH_PR = {
    "pr_url": "INSERT INTO lifecycle_step (request_id, step, position, state, updated_at, pr_url) VALUES (%s, 'deploy', 1, 'planned', now(), %s)",
    "head_sha": "INSERT INTO lifecycle_step (request_id, step, position, state, updated_at, head_sha) VALUES (%s, 'deploy', 1, 'planned', now(), %s)",
    "base_sha": "INSERT INTO lifecycle_step (request_id, step, position, state, updated_at, base_sha) VALUES (%s, 'deploy', 1, 'planned', now(), %s)",
}


@pytest.mark.parametrize("column, value", [("pr_url", "https://github.com/x/y/pull/7"), ("head_sha", "2" * 40), ("base_sha", "1" * 40)])
def test_a_planned_step_has_no_pull_request(pg_db, column, value):
    pid = person(pg_db, "admin")
    with conn_as(pg_db, "portal_web") as c:
        lifecycle(c, rid(1), pid)
    writing(pg_db, rid(1))
    with conn_as(pg_db, "portal_writer") as c:
        with pytest.raises(errors.CheckViolation):
            c.execute(PLANNED_WITH_PR[column], (rid(1), value))


# ---- the collector's write-back (spec 2026-11-02 L11) as the collector's role ----

def test_the_collector_sends_the_outbox_as_its_own_role(pg_db):
    import httpx
    from portal_fakes import mock_servicenow

    from portal.collectors.writeback import collect_writeback
    from portal.store.postgres import PostgresStore
    pid = person(pg_db, "admin")
    web, writer, collector = (PostgresStore(pg_db(role)) for role in ("portal_web", "portal_writer", "portal_collector"))
    try:
        final = {**FINAL, "task_sys_id": "5ca1ab1e000040008000000000010001"}  # the mock's SCTASK0010001
        req = web.create_lifecycle(request_id=rid(1), person_id=pid, repo="acme/notes", final=final, now=NOW,
                                   note="Requested in the golden road portal by admin: suspend the app.")
        writer.claim_approved(now=NOW)
        writer.add_lifecycle_event(request_id=req.id, step="pr_opened", note="Pull request #7 opened.", now=NOW)
        mock = mock_servicenow()
        inner, down = mock.transport(), {"on": True}
        mock.transport = lambda: httpx.MockTransport(
            lambda r: httpx.Response(503, json={}) if down["on"] and b"#7" in r.content else inner.handle_request(r))
        assert collect_writeback(collector, mock, base_url="https://portal.example.test", now=NOW) == {"sent": 1, "failed": 1, "waiting": 0}
        [kept] = collector.lifecycle_events(unsent=True)
        assert (kept.step, kept.attempts, kept.last_error) == ("pr_opened", 1, "ServiceNow answered 503 for the task's work note")
        down["on"] = False
        assert collect_writeback(collector, mock, base_url="https://portal.example.test", now=NOW)["sent"] == 1
        assert [e.sent_at is not None for e in web.lifecycle_events(req.id)] == [True, True]
        assert [kind for kind, _ in mock.task_history("SCTASK0010001")] == ["work_notes", "work_notes"]
    finally:
        for s in (web, writer, collector):
            s.close()


def test_one_write_back_pass_at_a_time_on_the_collector_s_advisory_lock(pg_db):
    """Two collectors (or a --once beside the pod): while one holds the write-back lock the other's pass sends nothing; the lock is
    released when the pass ends, also when it raises, so the next pass takes it."""
    from portal_fakes import mock_servicenow

    from portal.collectors.writeback import collect_writeback
    from portal.store.postgres import PostgresStore
    pid = person(pg_db, "admin")
    web, one, other = (PostgresStore(pg_db(role)) for role in ("portal_web", "portal_collector", "portal_collector"))
    try:
        final = {**FINAL, "task_sys_id": "5ca1ab1e000040008000000000010001"}
        web.create_lifecycle(request_id=rid(1), person_id=pid, repo="acme/notes", final=final, now=NOW,
                             note="Requested in the golden road portal by admin: suspend the app.")
        mock = mock_servicenow()
        with one.writeback_lock() as held:
            assert held is True
            with other.writeback_lock() as also:
                assert also is False
            assert collect_writeback(other, mock, base_url="https://portal.example.test", now=NOW) == {
                "sent": 0, "failed": 0, "waiting": 0, "busy": True}
        assert mock.task_history("SCTASK0010001") == []
        with pytest.raises(RuntimeError):
            with one.writeback_lock() as held:
                assert held is True
                raise RuntimeError("the pass failed")
        assert collect_writeback(other, mock, base_url="https://portal.example.test", now=NOW) == {"sent": 1, "failed": 0, "waiting": 0}
        with one.writeback_lock() as held:
            assert held is True
    finally:
        for s in (web, one, other):
            s.close()


def test_the_collector_writes_back_an_edited_ci_and_notes_it_as_its_own_role(pg_db):
    """Rule 9 on Postgres: the collector reads the state (its one lifecycle read besides the outbox), corrects the CI, and adds the
    ci_corrected note; a decommissioned app's date item closes (the intake part reads the state too)."""
    from portal_fakes import mock_servicenow

    from portal.collectors.cmdb import collect_cmdb
    from portal.collectors.intake import DECOMMISSIONED_NOTE, sync_date_items
    from portal.store.base import IntakeObserved
    from portal.store.postgres import PostgresStore
    pid = person(pg_db, "admin")
    web, writer, collector = (PostgresStore(pg_db(role)) for role in ("portal_web", "portal_writer", "portal_collector"))
    try:
        req = web.create_lifecycle(request_id=rid(1), person_id=pid, repo="acme/notes", final=FINAL, now=NOW,
                                   note="Requested in the golden road portal by admin: suspend the app.")
        writer.claim_approved(now=NOW)
        writer.add_lifecycle_steps(request_id=req.id, steps=ACTION_STEPS["suspend"], now=NOW)
        writer.move_lifecycle_step(request_id=req.id, step="deploy", to_state="done", now=NOW)
        writer.apply_lifecycle(request_id=req.id, merge_sha=None, note="Suspended.", now=NOW)
        collector.replace_intake_observed([IntakeObserved("acme/notes", "c" * 40, "intake/v1", False, "team", "low", "public", "Notes.",
                                                          None, None, None)], observed_at=NOW)
        mock = mock_servicenow()
        assert collect_cmdb(collector, mock, github_web="https://github.com", now=NOW)["synced"] == 1
        mock.edit_ci("acme/notes", u_golden_road_state="active")
        got = collect_cmdb(collector, mock, github_web="https://github.com", now=NOW)
        assert (got["synced"], got["corrected"]) == (1, 1)
        corrected = [e for e in web.lifecycle_events(req.id) if e.step == "ci_corrected"]
        assert len(corrected) == 1 and corrected[0].task_sys_id == FINAL["task_sys_id"] and "(u_golden_road_state)" in corrected[0].note
        other = decommission(pg_db, pid, repo="acme/tools")
        assert collector.lifecycle_state("acme/tools").request == other
        rows = [IntakeObserved("acme/tools", "d" * 40, "intake/v2", True, pid, "low", "public", "Tools.", dt.date(2026, 10, 1), None, None)]
        item = collector.open_rereview_item(repo="acme/tools", reason="review_overdue", key="2026-10-01", now=NOW)
        assert sync_date_items(collector, rows, today=NOW.date(), due_soon_days=30, now=NOW) == (0, 1)
        assert collector.rereview_items() == [] and web.audit_for("rereview_item", item.id)[-1].reason == DECOMMISSIONED_NOTE
    finally:
        for s in (web, writer, collector):
            s.close()


# ---- the two hand-run procedures, run as the docs print them (platform-team 18, portal-admin's refused note) ----
# The SQL is read from the docs, so a change to either is tested here. The docs are in the source repo only: the portal's own repo
# has no docs/ and skips these.

DOCS = Path(__file__).resolve().parents[3] / "docs" / "roles"
needs_docs = pytest.mark.skipif(not DOCS.is_dir(), reason="the role docs are in the source repo only")
PLACEHOLDER = re.compile(r"<[^<>]+>")
REFUSED = "ServiceNow answered 403 for the task's work note"


def doc_sql(doc, action):
    """The one ```sql block of docs/roles/<doc> that writes the audit action, its fence's indent removed."""
    text = (DOCS / doc).read_text()
    blocks = [textwrap.dedent(b) for b in re.findall(r"^[ \t]*```sql\n(.*?)^[ \t]*```$", text, re.M | re.S)]
    [block] = [b for b in blocks if f"'{action}'" in b]
    return block


def statements(block):
    """The block's statements, each ending its line with ';' (a reason may hold a ';' of its own)."""
    return [s.strip() for s in re.split(r";[ \t]*$", block, flags=re.M) if s.strip()]


def run_procedure(dsn, block, values, on_select=None):
    """Runs the block statement by statement in one psql-like session, each placeholder filled as the operator would. Returns the
    status of each statement; on an error rolls the transaction back and raises it."""
    done = []
    with psycopg.connect(dsn, autocommit=True) as c:
        try:
            for statement in statements(block):
                for placeholder, value in values.items():
                    statement = statement.replace(placeholder, value)
                assert not PLACEHOLDER.search(statement), statement
                cur = c.execute(statement)
                done.append(cur.statusmessage)
                if on_select and statement.startswith("SELECT"):
                    on_select(cur.fetchall(), values)
        except psycopg.Error:
            c.execute("ROLLBACK")
            raise
    return done


def audit_rows(pg_db, action):
    with conn_as(pg_db, "portal_owner") as c:
        return c.execute("SELECT actor_kind, actor_id, subject_kind, subject_id, before, after, reason FROM audit_log WHERE action = %s",
                         (action,)).fetchall()


def decommissioned_through_the_store(pg_db, pid, repo="acme/notes"):
    from portal.store.postgres import PostgresStore
    web, writer = PostgresStore(pg_db("portal_web")), PostgresStore(pg_db("portal_writer"))
    try:
        req = web.create_lifecycle(request_id=rid(1), person_id=pid, repo=repo, final={**FINAL, "action": "decommission"}, now=NOW,
                                   note="Requested in the golden road portal by admin: decommission the app.")
        writer.claim_approved(now=NOW)
        writer.add_lifecycle_steps(request_id=req.id, steps=ACTION_STEPS["decommission"], now=NOW)
        for step in ACTION_STEPS["decommission"]:
            writer.move_lifecycle_step(request_id=req.id, step=step, to_state="done", now=NOW)
        writer.apply_lifecycle(request_id=req.id, merge_sha=None, note="Decommissioned: the task is closed.", now=NOW)
        assert web.lifecycle_state(repo).state == "decommissioned"
        return req.id
    finally:
        web.close()
        writer.close()


@needs_docs
def test_platform_team_18_brings_a_decommissioned_app_back_as_the_owner_only(pg_db):
    from portal.store.base import Conflict
    from portal.store.postgres import PostgresStore
    block = doc_sql("platform-team.md", "lifecycle.brought_back")
    pid = person(pg_db, "admin")
    request_id = decommissioned_through_the_store(pg_db, pid, "Acme/Notes")
    values = {"<owner>/<repo>": "acme/notes", "<your person id>": pid, "<SCTASK...>": "SCTASK0010002", "<why>": "the team needs it again",
              "<url>": "https://github.com/acme/golden-road-factory/pull/9"}
    web = PostgresStore(pg_db("portal_web"))
    try:
        for role, refusal, words in (("portal_web", errors.InsufficientPrivilege, "lifecycle_state"),
                                     ("portal_writer", errors.RaiseException, "decommissioned for good"),
                                     ("portal_collector", errors.InsufficientPrivilege, "lifecycle_state")):
            with pytest.raises(refusal, match=words):
                run_procedure(pg_db(role), block, values)
            assert web.lifecycle_state("acme/notes").state == "decommissioned"
        assert audit_rows(pg_db, "lifecycle.brought_back") == []
        with pytest.raises(Conflict):
            web.create_registration(person_id=pid, draft=DRAFT, now=NOW)
        selected = []
        done = run_procedure(pg_db("portal_owner"), block, values, on_select=lambda rows, _: selected.extend(rows))
        assert done == ["BEGIN", "SELECT 1", "UPDATE 1", "INSERT 0 1", "COMMIT"]  # "The UPDATE must say UPDATE 1"
        assert [(r[0], r[1], r[4]) for r in selected] == [("Acme/Notes", "decommissioned", request_id)]
        state = web.lifecycle_state("acme/notes")
        assert (state.state, state.task, state.request) == ("active", None, request_id)
        [entry] = [e for e in web.audit_for("app", "acme/notes") if e.action == "lifecycle.brought_back"]
        assert (entry.actor_kind, entry.actor_id, entry.before, entry.after) == ("person", pid, {"state": "decommissioned"}, {"state": "active"})
        assert entry.reason == ("Brought back by hand for ServiceNow task SCTASK0010002: the team needs it again; unarchived, factory PR "
                                "https://github.com/acme/golden-road-factory/pull/9")
        assert entry in web.audit_entries()  # /admin/audit
        assert web.create_registration(person_id=pid, draft=DRAFT, now=NOW).state == "pending"  # the register form takes it again
        assert run_procedure(pg_db("portal_owner"), block, values)[2] == "UPDATE 0"  # run twice: nothing left to bring back
    finally:
        web.close()


@needs_docs
def test_portal_admin_marks_a_refused_note_sent_as_the_owner_only_and_the_later_notes_follow(pg_db):
    from portal.store.base import Conflict
    from portal.store.postgres import PostgresStore
    block = doc_sql("portal-admin.md", "lifecycle.event_marked_sent")
    pid = person(pg_db, "admin")
    web, writer, collector = (PostgresStore(pg_db(role)) for role in ("portal_web", "portal_writer", "portal_collector"))
    try:
        req = web.create_lifecycle(request_id=rid(1), person_id=pid, repo="acme/notes", final=FINAL, now=NOW,
                                   note="Requested in the golden road portal by admin: suspend the app.")
        writer.claim_approved(now=NOW)
        writer.add_lifecycle_steps(request_id=req.id, steps=ACTION_STEPS["suspend"], now=NOW)
        writer.move_lifecycle_step_with_event(request_id=req.id, step="deploy", to_state="pr_open", now=NOW, event="pr_opened",
                                              note="Pull request #7 opened.", branch="portal/lifecycle-x-deploy-1", pr_number=7,
                                              pr_url="https://github.com/acme/golden-road-deploy/pull/7", head_sha="2" * 40, base_sha="1" * 40)
        writer.move_lifecycle_step_with_event(request_id=req.id, step="deploy", to_state="done", now=NOW, event="merged",
                                              note="Pull request #7 merged.", merge_sha="3" * 40)
        writer.apply_lifecycle(request_id=req.id, merge_sha="3" * 40, note="Suspended: the task is closed.", now=NOW)
        requested, refused, merged, completed = collector.lifecycle_events(unsent=True)
        collector.mark_lifecycle_event_sent(event_id=requested.id, now=NOW)
        for _ in range(3):
            collector.mark_lifecycle_event_failed(event_id=refused.id, error=REFUSED, now=NOW)
        with pytest.raises(Conflict):  # the refused note blocks the request's later ones
            collector.mark_lifecycle_event_sent(event_id=merged.id, now=NOW)
        values = {"<request id>": req.id, "<your person id>": pid,
                  "<why, and who in ServiceNow confirmed it>": "the task is closed and its rules refuse notes; confirmed by the ServiceNow admins"}

        def oldest(rows, values):
            assert [r[:4] for r in rows] == [(refused.id, "pr_opened", 3, REFUSED), (merged.id, "merged", 0, None),
                                             (completed.id, "completed", 0, None)]
            values["<the oldest id above>"] = str(rows[0][0])

        # the collector may mark a note sent (its own work), but not write a person's audit entry: the transaction rolls back
        for role, refusal, words in (("portal_web", errors.InsufficientPrivilege, "lifecycle_events"),
                                     ("portal_writer", errors.InsufficientPrivilege, "lifecycle_events"),
                                     ("portal_collector", errors.RaiseException, "only system entries")):
            with pytest.raises(refusal, match=words):
                run_procedure(pg_db(role), block, dict(values), on_select=oldest)
            assert [e.id for e in collector.lifecycle_events(unsent=True)] == [refused.id, merged.id, completed.id]
        assert audit_rows(pg_db, "lifecycle.event_marked_sent") == []
        done = run_procedure(pg_db("portal_owner"), block, dict(values), on_select=oldest)
        assert done == ["BEGIN", "SELECT 3", "UPDATE 1", "INSERT 0 1", "COMMIT"]
        [entry] = [e for e in web.audit_for("request", req.id) if e.action == "lifecycle.event_marked_sent"]
        assert (entry.actor_kind, entry.actor_id, entry.before, entry.after) == ("person", pid, None, {"event": refused.id})
        assert entry.reason.startswith("ServiceNow refuses this note for good: the task is closed") and entry in web.audit_entries()
        for event in (merged, completed):  # the collector's next pass sends the rest, in order
            collector.mark_lifecycle_event_sent(event_id=event.id, now=NOW)
        assert collector.lifecycle_events(unsent=True) == []
        assert [(e.step, e.sent_at is not None) for e in web.lifecycle_events(req.id)] == [
            ("requested", True), ("pr_opened", True), ("merged", True), ("completed", True)]
    finally:
        for s in (web, writer, collector):
            s.close()
