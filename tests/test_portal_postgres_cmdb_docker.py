"""Migration 0011 (spec 2026-10-31 Part 3): the CMDB record only the collector writes and nobody deletes, the collector's view of person
(id and email, nothing else), the registration's ownership acknowledgement that the web can't change, and the cmdb part's problems."""

import datetime as dt
import uuid

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.types.json import Jsonb
from portal_fakes import evidence_doc
from portal_pg import pg_db, pg_server  # noqa: F401

pytestmark = pytest.mark.docker
NOW = dt.datetime(2026, 10, 31, 12, tzinfo=dt.UTC)
SYNC = ("INSERT INTO cmdb_record (repo, sys_id, number, fields_hash, last_operation, last_synced_at) "
        "VALUES ('acme/notes', %s, 'APM0001001', %s, 'INSERT', now())")


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


def test_only_the_collector_writes_the_cmdb_record_and_the_web_reads_it(pg_db):
    with conn_as(pg_db, "portal_collector") as c:
        c.execute(SYNC, ("c" * 32, "f" * 64))
        c.execute("UPDATE cmdb_record SET last_error = 'cmdb_unreachable', last_error_at = now()")
    with conn_as(pg_db, "portal_web") as c:
        assert c.execute("SELECT number FROM cmdb_record").fetchone()[0] == "APM0001001"
        for statement in ("INSERT INTO cmdb_record (repo, last_error, last_error_at) VALUES ('acme/x', 'cmdb_refused', now())",
                          "UPDATE cmdb_record SET last_error = NULL"):
            with pytest.raises(errors.InsufficientPrivilege):
                c.execute(statement)
    with conn_as(pg_db, "portal_writer") as c:
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("SELECT 1 FROM cmdb_record")


@pytest.mark.parametrize("role", ["portal_owner", "portal_collector"])
@pytest.mark.parametrize("statement", ["DELETE FROM cmdb_record", "TRUNCATE cmdb_record"])
def test_nobody_deletes_a_cmdb_record_not_even_the_owner(pg_db, role, statement):
    with conn_as(pg_db, "portal_collector") as c:
        c.execute(SYNC, ("c" * 32, "f" * 64))
    with conn_as(pg_db, role) as c:
        with pytest.raises((errors.RaiseException, errors.InsufficientPrivilege)) as err:
            c.execute(statement)
        if role == "portal_owner":
            assert "cmdb_record is insert-only" in str(err.value)
        assert c.execute("SELECT count(*) FROM cmdb_record").fetchone()[0] == 1


def test_the_collector_reads_a_person_s_id_and_email_and_nothing_else(pg_db):
    pid = person(pg_db, "o", email="owner@example.test")
    with conn_as(pg_db, "portal_collector") as c:
        assert c.execute("SELECT id, email FROM person WHERE id = %s", (pid,)).fetchone() == (pid, "owner@example.test")
        for statement in ("SELECT display_name FROM person", "SELECT * FROM person", "UPDATE person SET email = 'x@example.test'"):
            with pytest.raises(errors.InsufficientPrivilege):
                c.execute(statement)


def test_the_web_can_t_change_an_acknowledgement_and_a_binding_has_none(pg_db):
    from portal.store.postgres import PostgresStore
    owner = person(pg_db, "o")
    web = PostgresStore(pg_db("portal_web"))
    try:
        draft = {"schema": "registration-draft/v1", "repo": "acme/notes", "purpose": "Stores short team notes.", "data_classification": "public"}
        req = web.create_registration(person_id=owner, draft=draft, now=NOW, ownership_acknowledged_at=NOW)
    finally:
        web.close()
    with conn_as(pg_db, "portal_web") as c:
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("UPDATE request SET ownership_acknowledged_at = now() WHERE id = %s", (req.id,))
    with conn_as(pg_db, "portal_writer") as c:
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("UPDATE request SET ownership_acknowledged_at = NULL WHERE id = %s", (req.id,))
    with conn_as(pg_db, "portal_owner") as c:
        assert c.execute("SELECT ownership_acknowledged_at FROM request WHERE id = %s", (req.id,)).fetchone()[0] == req.ownership_acknowledged_at


def test_the_acknowledgement_s_time_is_the_database_s(pg_db):
    """0011's insert guard: a non-NULL ownership_acknowledged_at is the database's now(), whatever the insert says; NULL stays NULL."""
    import datetime as dt
    from portal.store.postgres import PostgresStore
    owner = person(pg_db, "o")
    forged = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    web = PostgresStore(pg_db("portal_web"))
    try:
        draft = {"schema": "registration-draft/v1", "repo": "acme/notes", "purpose": "Stores short team notes.", "data_classification": "public"}
        req = web.create_registration(person_id=owner, draft=draft, now=NOW, ownership_acknowledged_at=forged)
        other = web.create_registration(person_id=owner, draft={**draft, "repo": "acme/other"}, now=NOW)
    finally:
        web.close()
    with conn_as(pg_db, "portal_owner") as c:
        db_now = c.execute("SELECT now()").fetchone()[0]
        stored = c.execute("SELECT ownership_acknowledged_at FROM request WHERE id = %s", (req.id,)).fetchone()[0]
        assert stored != forged and abs(db_now - stored) < dt.timedelta(minutes=5) and stored == req.ownership_acknowledged_at
        assert c.execute("SELECT ownership_acknowledged_at FROM request WHERE id = %s", (other.id,)).fetchone()[0] is None


def test_a_binding_request_with_an_acknowledgement_is_refused(pg_db):
    from test_portal_store_contract import binding_final
    owner, rid = person(pg_db, "o"), "r_" + "1" * 32
    final = Jsonb(binding_final("acme/notes", owner, rid))
    insert = ("INSERT INTO request (id, kind, repo, state, draft, final, requested_by, created_at, updated_at, ownership_acknowledged_at) "
              "VALUES (%s, 'binding', 'acme/notes', 'approved', %s, %s, %s, now(), now(), %s)")
    with conn_as(pg_db, "portal_web") as c:
        with pytest.raises(errors.CheckViolation):
            c.execute(insert, (rid, final, final, owner, NOW))
        c.execute(insert, (rid, final, final, owner, None))   # the same binding without one is fine


def test_an_error_keeps_the_ci_even_when_an_update_tries_to_change_it_and_the_times_are_the_database_s(pg_db):
    with conn_as(pg_db, "portal_collector") as c:
        c.execute("INSERT INTO cmdb_record (repo, sys_id, number, fields_hash, last_operation, last_synced_at, last_error_at) "
                  "VALUES ('acme/notes', %s, 'APM0001001', %s, 'INSERT', '2000-01-01', '2000-01-01')", ("c" * 32, "f" * 64))
        synced, error_at = c.execute("SELECT last_synced_at, last_error_at FROM cmdb_record").fetchone()
        assert synced > NOW - dt.timedelta(days=3650) and error_at is None   # a good sync has no error time
        c.execute("UPDATE cmdb_record SET last_error = 'cmdb_unreachable', last_error_at = '2000-01-01', sys_id = %s, number = 'APM0009999', "
                  "fields_hash = %s, last_operation = 'UPDATE', last_synced_at = '2000-01-01'", ("d" * 32, "e" * 64))
        row = c.execute("SELECT sys_id, number, fields_hash, last_operation, last_synced_at, last_error, last_error_at FROM cmdb_record").fetchone()
        assert row[:6] == ("c" * 32, "APM0001001", "f" * 64, "INSERT", synced, "cmdb_unreachable") and row[6] > NOW - dt.timedelta(days=3650)


@pytest.mark.parametrize("values", [
    ("acme/notes", None, None, None, None, None),                               # neither a CI nor an error
    ("acme/notes", "c" * 32, None, "f" * 64, "INSERT", None),                   # a CI without its number
    ("acme/notes", "C" * 32, "APM0001001", "f" * 64, "INSERT", None),           # a sys_id in upper case
    ("acme/notes", "c" * 32, "APM0001001", "f" * 64, "DELETE", None),           # an unknown operation
    ("acme/notes", None, None, None, None, "failed"),                           # not a CMDB error
    ("not a repo", None, None, None, None, "cmdb_refused"),
])
def test_the_table_refuses_a_malformed_row(pg_db, values):
    with conn_as(pg_db, "portal_collector") as c:
        with pytest.raises(errors.CheckViolation):
            c.execute("INSERT INTO cmdb_record (repo, sys_id, number, fields_hash, last_operation, last_error, last_synced_at, last_error_at) "
                      "VALUES (%s, %s, %s, %s, %s, %s, now(), now())", values)


def test_one_cmdb_record_per_repo_in_any_case(pg_db):
    with conn_as(pg_db, "portal_collector") as c:
        c.execute("INSERT INTO cmdb_record (repo, last_error) VALUES ('acme/notes', 'cmdb_owner_unknown')")
        with pytest.raises(errors.UniqueViolation):
            c.execute("INSERT INTO cmdb_record (repo, last_error) VALUES ('ACME/notes', 'cmdb_owner_unknown')")


def test_the_cmdb_part_s_problems_are_its_own(pg_db):
    with conn_as(pg_db, "portal_collector") as c:
        for problem in ("cmdb_unreachable", "cmdb_owner_unknown", "cmdb_refused", "failed", None):
            c.execute("INSERT INTO rereview_source (source, problem, counts, checked_at) VALUES ('cmdb', %s, '{}', now()) "
                      "ON CONFLICT (source) DO UPDATE SET problem = EXCLUDED.problem", (problem,))
        for source, problem in (("cmdb", "factory_unreadable"), ("intake", "cmdb_unreachable"), ("other", None)):
            with pytest.raises(errors.CheckViolation):
                c.execute("INSERT INTO rereview_source (source, problem, counts, checked_at) VALUES (%s, %s, '{}', now()) "
                          "ON CONFLICT (source) DO UPDATE SET problem = EXCLUDED.problem", (source, problem))


def test_each_process_role_does_its_cmdb_work_through_the_store(pg_db):
    from portal.store.postgres import PostgresStore
    pid = person(pg_db, "o", email="owner@example.test")
    web, collector = PostgresStore(pg_db("portal_web")), PostgresStore(pg_db("portal_collector"))
    try:
        assert collector.person_email(pid) == "owner@example.test" and collector.person_email("p_" + "0" * 32) is None
        collector.record_cmdb_error(repo="acme/tools", error="cmdb_owner_unknown", now=NOW)
        collector.record_cmdb_sync(repo="acme/notes", sys_id="c" * 32, number="APM0001001", fields_hash="f" * 64, operation="INSERT", now=NOW)
        collector.record_cmdb_error(repo="acme/notes", error="cmdb_unreachable", now=NOW)
        collector.set_rereview_status(source="cmdb", problem="cmdb_unreachable", counts={"synced": 0, "unreachable": 1}, now=NOW)
        assert [(r.repo, r.number, r.last_error) for r in web.cmdb_records()] == [("acme/notes", "APM0001001", "cmdb_unreachable"),
                                                                                  ("acme/tools", None, "cmdb_owner_unknown")]
        assert web.cmdb_record("ACME/NOTES").sys_id == "c" * 32 and web.rereview_status()["cmdb"].problem == "cmdb_unreachable"
        with pytest.raises(errors.InsufficientPrivilege):
            web.record_cmdb_error(repo="acme/notes", error="cmdb_refused", now=NOW)
    finally:
        web.close()
        collector.close()


def test_0011_applies_to_a_populated_database_migrated_through_0010(pg_server, monkeypatch):
    """Forward only, safe on existing rows: requests, evidence, observed records and a status row for each part survive; the old
    requests have no acknowledgement; the new source and problems are accepted; apply is then idempotent."""
    from portal import migrate
    from portal.store.base import IntakeObserved
    from portal.store.postgres import PostgresStore
    every = migrate.migrations()
    name = f"portal_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server.dsn(), autocommit=True) as conn:
        conn.execute(sql.Composed([sql.SQL("CREATE DATABASE "), sql.Identifier(name), sql.SQL(" OWNER portal_owner")]))
    dsn = lambda role="portal_owner": pg_server.dsn(role, name)  # noqa: E731
    try:
        monkeypatch.setattr(migrate, "migrations", lambda: [p for p in every if p.stem[:4] <= "0010"])
        assert migrate.apply(dsn())[-1] == "0010_registration_changes_and_rereview"
        store = PostgresStore(dsn())
        try:
            day = dt.timedelta(hours=8)
            owner, _, _ = store.sign_in(issuer="i", subject="o", display_name="O", email="o@example.test", groups=[], roles=[], now=NOW, max_age=day)
            draft = {"schema": "registration-draft/v1", "repo": "acme/notes", "purpose": "Stores short team notes.", "data_classification": "public"}
            with psycopg.connect(dsn("portal_web"), autocommit=True) as c:   # a registration as 0010 knew it (no acknowledgement column)
                old_id = "r_" + "a" * 32
                c.execute("INSERT INTO request (id, kind, repo, state, draft, requested_by, created_at, updated_at) "
                          "VALUES (%s, 'registration', 'acme/notes', 'pending', %s, %s, now(), now())", (old_id, Jsonb(draft), owner.id))
            store.add_evidence(request_id=old_id, submission=1, commit="e" * 40, evidence=evidence_doc("acme/notes"), floor_tier="low",
                               floor_classification="public", rules_commit="f" * 40, now=NOW)
            store.replace_intake_observed([IntakeObserved("acme/notes", "c" * 40, "intake/v2", True, owner.id, "low", "public",
                                                          "Stores notes.", dt.date(2027, 10, 1), None, None)], observed_at=NOW)
            for source, problem in (("repo_admins", None), ("evidence", "failed"), ("intake", "factory_unreadable"), ("events", "github_busy")):
                store.set_rereview_status(source=source, problem=problem, counts={"n": 1}, now=NOW)
            monkeypatch.setattr(migrate, "migrations", lambda: [p for p in every if p.stem[:4] <= "0011"])  # 0012 has its own test
            assert migrate.apply(dsn()) == ["0011_mcp_skills_and_cmdb"] and migrate.apply(dsn()) == []
            old = store.get_request(old_id)
            assert old.state == "pending" and old.ownership_acknowledged_at is None
            assert store.evidence_for(old_id, 1).commit == "e" * 40 and [r.repo for r in store.intake_observed()] == ["acme/notes"]
            status = store.rereview_status()
            assert {s: (v.problem, v.counts) for s, v in status.items()} == {
                "repo_admins": (None, {"n": 1}), "evidence": ("failed", {"n": 1}), "intake": ("factory_unreadable", {"n": 1}),
                "events": ("github_busy", {"n": 1})}
            store.set_rereview_status(source="cmdb", problem="cmdb_owner_unknown", counts={}, now=NOW)
            store.set_rereview_status(source="events", problem="actions_unreadable", counts={}, now=NOW)  # the old problems still hold
            assert store.cmdb_records() == [] and store.person_email(owner.id) == "o@example.test"
        finally:
            store.close()
    finally:
        with psycopg.connect(pg_server.dsn(), autocommit=True) as conn:
            conn.execute(sql.Composed([sql.SQL("DROP DATABASE "), sql.Identifier(name), sql.SQL(" WITH (FORCE)")]))
