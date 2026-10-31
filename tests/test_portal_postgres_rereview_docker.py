"""Migration 0010 (spec 2026-10-31): the change's record on the evidence, the observed caches only the collector writes, and the re-review
items: opened by the collector, closed once (an admin with a note, or the platform for a date), never reopened, never deleted."""

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
ITEM = "i_" + "1" * 32
OBSERVED = ("INSERT INTO intake_observed (repo, blob_sha, schema, by_portal, owner, tier, data_classification, purpose, review_by, "
            "observed_at) VALUES ('acme/notes', %s, 'intake/v2', true, %s, 'low', 'public', 'Stores notes.', '2026-10-01', now())")


def conn_as(pg_db, role):
    return psycopg.connect(pg_db(role), autocommit=True)


def person(pg_db, subject):
    from portal.store.postgres import PostgresStore
    store = PostgresStore(pg_db("portal_web"))
    try:
        found, _, _ = store.sign_in(issuer="i", subject=subject, display_name=subject, email=None, groups=[], roles=[], now=NOW,
                                    max_age=dt.timedelta(hours=8))
        return found.id
    finally:
        store.close()


def open_item(pg_db, reason="review_overdue", key="2026-10-01"):
    with conn_as(pg_db, "portal_collector") as c:
        c.execute("INSERT INTO rereview_item (id, repo, reason, key, opened_at) VALUES (%s, 'acme/notes', %s, %s, now())", (ITEM, reason, key))


def test_only_the_collector_writes_the_caches(pg_db):
    owner = person(pg_db, "o")
    with conn_as(pg_db, "portal_collector") as c:
        c.execute(OBSERVED, ("c" * 40, owner))
        c.execute("INSERT INTO rereview_run (repo, run_id, outcome, seen_at) VALUES ('acme/notes', 1, 'invalid', now())")
        c.execute("INSERT INTO rereview_source (source, problem, counts, checked_at) VALUES ('events', 'actions_unreadable', '{}', now())")
        c.execute("DELETE FROM intake_observed")
    with conn_as(pg_db, "portal_web") as c:
        for statement in ("INSERT INTO intake_observed (repo, blob_sha, schema, by_portal, problem, observed_at) "
                          "VALUES ('acme/x', '" + "c" * 40 + "', NULL, false, 'invalid', now())",
                          "INSERT INTO rereview_run (repo, run_id, outcome, seen_at) VALUES ('acme/notes', 2, 'event', now())",
                          "UPDATE rereview_source SET problem = NULL", "DELETE FROM intake_observed"):
            with pytest.raises(errors.InsufficientPrivilege):
                c.execute(statement)
        assert c.execute("SELECT count(*) FROM rereview_run").fetchone()[0] == 1   # the web reads them
    with conn_as(pg_db, "portal_writer") as c:
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("SELECT 1 FROM intake_observed")


def seed_history(pg_db):
    """One row in every history table, so a row trigger has something to refuse."""
    with conn_as(pg_db, "portal_collector") as c:
        c.execute("INSERT INTO rereview_run (repo, run_id, outcome, seen_at) VALUES ('acme/notes', 1, 'event', now())")
        c.execute("INSERT INTO rereview_event (repo, head_sha, pr, level, floor, complete, created, tree_sha, scorecard_version, signals, "
                  "run_repo, run_id, observed_at) VALUES ('acme/notes', %s, 7, 'elevated', 'standard', true, now(), %s, '0.5.1', '[]', "
                  "'acme/scorecard-factory', 1, now())", ("a" * 40, "b" * 40))
        c.execute("INSERT INTO rereview_item (id, repo, reason, key, opened_at) VALUES (%s, 'acme/notes', 'scorecard_signal', %s, now())",
                  (ITEM, "a" * 40))
        c.execute("INSERT INTO rereview_item_event (item_id, repo, head_sha, matched, added_at) VALUES (%s, 'acme/notes', %s, '[\"T-17\"]', now())",
                  (ITEM, "a" * 40))


@pytest.mark.parametrize("statement", ["UPDATE rereview_run SET outcome = 'invalid'", "DELETE FROM rereview_run", "TRUNCATE rereview_run CASCADE",
                                       "UPDATE rereview_event SET level = 'standard'", "DELETE FROM rereview_event",
                                       "DELETE FROM rereview_item", "TRUNCATE rereview_item CASCADE", "DELETE FROM rereview_item_event",
                                       "UPDATE rereview_item_event SET matched = '[\"T-18\"]'"])
def test_nobody_rewrites_the_history_not_even_the_owner(pg_db, statement):
    seed_history(pg_db)
    with pytest.raises(errors.RaiseException):
        with conn_as(pg_db, "portal_owner") as c:
            c.execute(statement)


def test_an_item_is_closed_once_and_never_reopened(pg_db):
    admin = person(pg_db, "a")
    open_item(pg_db)
    with conn_as(pg_db, "portal_web") as c:
        with pytest.raises(errors.CheckViolation):  # a person's close needs a note of 10 to 1000 characters on one line
            c.execute("UPDATE rereview_item SET closed_at = now(), close_kind = 'person', closed_by = %s, close_note = 'short' WHERE id = %s",
                      (admin, ITEM))
        with pytest.raises(errors.RaiseException):  # the web closes as a person, never as the platform
            c.execute("UPDATE rereview_item SET closed_at = now(), close_kind = 'auto', close_note = 'by the platform here' WHERE id = %s", (ITEM,))
        c.execute("UPDATE rereview_item SET closed_at = now(), close_kind = 'person', closed_by = %s, close_note = 'Renewal is on its way.' "
                  "WHERE id = %s", (admin, ITEM))
        with pytest.raises(errors.RaiseException):
            c.execute("UPDATE rereview_item SET closed_at = NULL, close_kind = NULL, closed_by = NULL, close_note = NULL WHERE id = %s", (ITEM,))
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("UPDATE rereview_item SET key = '2027-01-01' WHERE id = %s", (ITEM,))
    with conn_as(pg_db, "portal_collector") as c:
        with pytest.raises(errors.UniqueViolation):  # the same repo, reason and date: never a second item
            c.execute("INSERT INTO rereview_item (id, repo, reason, key, opened_at) VALUES (%s, 'ACME/notes', 'review_overdue', '2026-10-01', now())",
                      ("i_" + "2" * 32,))


def test_an_admin_cannot_close_an_item_for_an_app_they_own_even_by_a_direct_update(pg_db):
    owner = person(pg_db, "o")
    with conn_as(pg_db, "portal_collector") as c:
        c.execute(OBSERVED, ("c" * 40, owner))
    open_item(pg_db)
    with conn_as(pg_db, "portal_web") as c:
        with pytest.raises(errors.RaiseException, match="owns"):
            c.execute("UPDATE rereview_item SET closed_at = now(), close_kind = 'person', closed_by = %s, close_note = 'I own it, closing.' "
                      "WHERE id = %s", (owner, ITEM))


def test_the_collector_closes_only_date_items_and_only_as_the_platform(pg_db):
    open_item(pg_db, reason="review_due_soon", key="2026-11-01")
    with conn_as(pg_db, "portal_collector") as c:
        with pytest.raises(errors.RaiseException):
            c.execute("UPDATE rereview_item SET closed_at = now(), close_kind = 'person', closed_by = NULL, close_note = 'not mine to say' "
                      "WHERE id = %s", (ITEM,))
        c.execute("UPDATE rereview_item SET closed_at = now(), close_kind = 'auto', close_note = 'The date moved to 2027-11-01.' WHERE id = %s",
                  (ITEM,))


def test_an_event_joins_only_an_open_scorecard_item_of_its_own_repo(pg_db):
    with conn_as(pg_db, "portal_collector") as c:
        for head in ("a", "d"):
            c.execute("INSERT INTO rereview_event (repo, head_sha, pr, level, floor, complete, created, tree_sha, scorecard_version, signals, "
                      "run_repo, run_id, observed_at) VALUES ('acme/notes', %s, 7, 'elevated', 'standard', true, now(), %s, '0.5.1', '[]', "
                      "'acme/scorecard-factory', %s, now())", (head * 40, "b" * 40, ord(head)))
        c.execute("INSERT INTO rereview_item (id, repo, reason, key, opened_at) VALUES (%s, 'acme/notes', 'scorecard_signal', %s, now())",
                  (ITEM, "a" * 40))
        c.execute("INSERT INTO rereview_item_event (item_id, repo, head_sha, matched, added_at) VALUES (%s, 'acme/notes', %s, '[\"T-17\"]', now())",
                  (ITEM, "a" * 40))
        with pytest.raises(errors.ForeignKeyViolation):   # an event the cache doesn't hold
            c.execute("INSERT INTO rereview_item_event (item_id, repo, head_sha, matched, added_at) VALUES (%s, 'acme/notes', %s, '[\"T-17\"]', now())",
                      (ITEM, "e" * 40))
    with conn_as(pg_db, "portal_web") as c:
        admin = person(pg_db, "a")
        c.execute("UPDATE rereview_item SET closed_at = now(), close_kind = 'person', closed_by = %s, close_note = 'Reviewed with the owner.' "
                  "WHERE id = %s", (admin, ITEM))
    with conn_as(pg_db, "portal_collector") as c:
        with pytest.raises(errors.RaiseException, match="open"):
            c.execute("INSERT INTO rereview_item_event (item_id, repo, head_sha, matched, added_at) VALUES (%s, 'acme/notes', %s, '[\"T-17\"]', now())",
                      (ITEM, "d" * 40))


def test_the_collector_audits_only_as_the_system_what_it_does_to_an_item_that_exists(pg_db):
    open_item(pg_db)
    audit = "INSERT INTO audit_log (at, actor_kind, action, subject_kind, subject_id, reason) VALUES (now(), %s, %s, %s, %s, 'r')"
    with conn_as(pg_db, "portal_collector") as c:
        c.execute(audit, ("system", "rereview_item.opened", "rereview_item", ITEM))  # event_added and closed: the test below
        for kind, action, subject, subject_id in (("person", "rereview_item.opened", "rereview_item", ITEM),
                                                  ("system", "rereview_item.opened", "request", ITEM),
                                                  ("system", "request.approved", "rereview_item", ITEM),
                                                  ("system", "rereview_item.opened", "rereview_item", "i_" + "9" * 32)):
            with pytest.raises(errors.RaiseException):
                c.execute(audit, (kind, action, subject, subject_id))
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("SELECT 1 FROM audit_log")


def test_evidence_carries_its_record_whole_or_not_at_all(pg_db):
    from portal.store.postgres import PostgresStore
    owner = person(pg_db, "o")
    store = PostgresStore(pg_db("portal_web"))
    try:
        req = store.create_registration(person_id=owner, draft={"schema": "registration-draft/v1", "repo": "acme/notes"}, now=NOW)
    finally:
        store.close()
    insert = ("INSERT INTO registration_evidence (request_id, submission, commit_sha, evidence, floor_tier, floor_classification, rules_commit, "
              "computed_at, record_sha, record) VALUES (%s, 1, %s, %s, 'low', 'public', %s, now(), %s, %s)")
    with conn_as(pg_db, "portal_collector") as c:
        for sha, record in (("c" * 40, None), (None, Jsonb({"schema": "intake/v2"})), ("c" * 40, Jsonb({"schema": "intake/v9"}))):
            with pytest.raises(errors.CheckViolation):
                c.execute(insert, (req.id, "e" * 40, Jsonb(evidence_doc("acme/notes")), "f" * 40, sha, record))
        c.execute(insert, (req.id, "e" * 40, Jsonb(evidence_doc("acme/notes")), "f" * 40, "c" * 40, Jsonb({"schema": "invalid"})))


def test_0010_applies_to_a_database_migrated_through_0009_that_has_requests_and_evidence(pg_server, monkeypatch):
    """Forward only, safe on existing rows: old evidence has no record and still approves; the new problem is accepted; the caches start
    empty; nothing else moved."""
    from portal import migrate
    from portal.store.postgres import PostgresStore
    every = migrate.migrations()
    name = f"portal_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server.dsn(), autocommit=True) as conn:
        conn.execute(sql.Composed([sql.SQL("CREATE DATABASE "), sql.Identifier(name), sql.SQL(" OWNER portal_owner")]))
    dsn = lambda role="portal_owner": pg_server.dsn(role, name)  # noqa: E731
    try:
        monkeypatch.setattr(migrate, "migrations", lambda: [p for p in every if p.stem[:4] <= "0009"])
        assert migrate.apply(dsn())[-1] == "0009_audit_log_subject_index"
        store = PostgresStore(dsn())
        try:
            day = dt.timedelta(hours=8)
            owner, _, _ = store.sign_in(issuer="i", subject="o", display_name="O", email=None, groups=[], roles=[], now=NOW, max_age=day)
            admin, _, _ = store.sign_in(issuer="i", subject="a", display_name="A", email=None, groups=["g"], roles=["admin"], now=NOW, max_age=day)
            draft = {"schema": "registration-draft/v1", "repo": "acme/notes", "purpose": "Stores short team notes.", "data_classification": "public"}
            old = store.create_registration(person_id=owner.id, draft=draft, now=NOW)
            with psycopg.connect(dsn("portal_collector"), autocommit=True) as c:   # evidence as 0009 knew it (no record columns yet)
                c.execute("INSERT INTO registration_evidence (request_id, submission, commit_sha, evidence, floor_tier, floor_classification, "
                          "rules_commit, computed_at) VALUES (%s, 1, %s, %s, 'low', 'public', %s, now())",
                          (old.id, "e" * 40, Jsonb(evidence_doc("acme/notes")), "f" * 40))
            waiting = store.create_registration(person_id=owner.id, draft={**draft, "repo": "acme/tools"}, now=NOW)
            store.add_evidence_problem(request_id=waiting.id, submission=1, problem="repo_unreadable", now=NOW)
            monkeypatch.setattr(migrate, "migrations", lambda: every)
            assert migrate.apply(dsn()) == ["0010_registration_changes_and_rereview"] and migrate.apply(dsn()) == []
            assert store.evidence_for(old.id, 1).record_sha is None and store.evidence_for(old.id, 1).record is None
            assert store.evidence_problem_for(waiting.id, 1) == "repo_unreadable"
            store.add_evidence_problem(request_id=waiting.id, submission=1, problem="record_unreadable", now=NOW)
            final = {"schema": "intake/v2", "tier": "moderate", "data_classification": "public", "approved_at": "2026-10-31T12:00:00Z",
                     "evidence_commit": "e" * 40, "floor_tier": "low", "floor_classification": "public"}
            assert store.decide(request_id=old.id, actor_id=admin.id, actor_groups=["g"], approve=True, note="ok", final=final,
                                now=NOW).state == "approved"
            assert store.intake_observed() == [] and store.rereview_items(open_only=False) == [] and store.rereview_status() == {}
        finally:
            store.close()
    finally:
        with psycopg.connect(pg_server.dsn(), autocommit=True) as conn:
            conn.execute(sql.Composed([sql.SQL("DROP DATABASE "), sql.Identifier(name), sql.SQL(" WITH (FORCE)")]))


def test_each_process_role_can_do_its_re_review_work_through_the_store(pg_db):
    from portal.store.base import IntakeObserved
    from portal.store.postgres import PostgresStore
    from test_portal_store_contract import event_doc
    web, collector = PostgresStore(pg_db("portal_web")), PostgresStore(pg_db("portal_collector"))
    try:
        admin, _, _ = web.sign_in(issuer="i", subject="a", display_name="A", email=None, groups=["g"], roles=["admin"], now=NOW,
                                  max_age=dt.timedelta(hours=8))
        collector.replace_intake_observed([IntakeObserved("acme/notes", "c" * 40, "intake/v2", True, "p_" + "9" * 32, "low", "public",
                                                          "Stores notes.", dt.date(2026, 10, 1), None, None)], observed_at=NOW)
        collector.record_rereview_run(repo="acme/scorecard-factory", run_id=5, outcome="event", now=NOW)
        collector.add_rereview_event(doc=event_doc(), run_repo="acme/scorecard-factory", run_id=5, now=NOW)
        item = collector.open_rereview_item(repo="acme/notes", reason="scorecard_signal", key="a" * 40, now=NOW, event=("a" * 40, ("T-17",)))
        collector.set_rereview_status(source="events", problem=None, counts={"event": 1}, now=NOW)
        assert [i.id for i in web.rereview_items()] == [item.id] and web.rereview_run_counts() == {"event": 1}
        web.close_rereview_item(item_id=item.id, actor_id=admin.id, note="Reviewed the T-17 change.", now=NOW, events_seen=1)
        assert web.rereview_items() == [] and web.rereview_status()["events"].counts == {"event": 1}
    finally:
        web.close()
        collector.close()


def test_an_approval_keeps_the_scorecard_mark_of_the_record_a_change_replaces_even_by_a_direct_update(pg_db):
    """check_approval's scorecard rule, held again by the approval guard as its other rules are."""
    from portal.store.postgres import PostgresStore
    store = PostgresStore(pg_db())
    try:
        day = dt.timedelta(hours=8)
        owner, _, _ = store.sign_in(issuer="i", subject="o", display_name="O", email=None, groups=[], roles=[], now=NOW, max_age=day)
        admin, _, _ = store.sign_in(issuer="i", subject="a", display_name="A", email=None, groups=["g"], roles=["admin"], now=NOW, max_age=day)
        req = store.create_registration(person_id=owner.id, draft={"schema": "registration-draft/v1", "repo": "acme/notes"}, now=NOW)
    finally:
        store.close()
    record = {"schema": "intake/v2", "by_portal": True, "tier": "low", "data_classification": "public", "purpose": "Stores notes.",
              "owner": owner.id, "review_by": "2027-01-01", "scorecard": "central"}
    with conn_as(pg_db, "portal_collector") as c:
        c.execute("INSERT INTO registration_evidence (request_id, submission, commit_sha, evidence, floor_tier, floor_classification, rules_commit, "
                  "computed_at, record_sha, record) VALUES (%s, 1, %s, %s, 'low', 'public', %s, now(), %s, %s)",
                  (req.id, "e" * 40, Jsonb(evidence_doc("acme/notes")), "f" * 40, "c" * 40, Jsonb(record)))
    final = {"schema": "intake/v2", "tier": "low", "data_classification": "public", "approved_at": "2026-10-31T12:00:00Z",
             "evidence_commit": "e" * 40, "floor_tier": "low", "floor_classification": "public"}
    decide = ("UPDATE request SET state = 'approved', updated_at = now(), decided_by = %s, decided_at = now(), decision_groups = '{g}', "
              "decision_note = 'checked', final = %s WHERE id = %s")
    with conn_as(pg_db, "portal_web") as c:
        for mark in (None, "per-app"):
            with pytest.raises(errors.RaiseException, match="scorecard"):
                c.execute(decide, (admin.id, Jsonb({**final, **({"scorecard": mark} if mark else {})}), req.id))
        c.execute(decide, (admin.id, Jsonb({**final, "scorecard": "central"}), req.id))


def test_an_owner_recorded_between_the_store_s_check_and_its_update_is_still_own_app(pg_db):
    """The collector may replace intake_observed while the web closes an item: the store's own check passes, the database's trigger
    refuses, and the store reports the same Conflict as its own check would."""
    import contextlib
    from portal.store.base import Conflict
    from portal.store.postgres import PostgresStore
    owner = person(pg_db, "o")
    open_item(pg_db)
    web = PostgresStore(pg_db("portal_web"))
    real = web.pool.connection

    class Racing:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, query, params=None):
            found = self.conn.execute(query, params)
            if query.startswith("SELECT 1 FROM intake_observed WHERE lower(repo) = lower(%(repo)s)"):   # the owner check ran: now the collector
                # records the owner
                with conn_as(pg_db, "portal_collector") as c:
                    c.execute(OBSERVED, ("c" * 40, owner))
            return found

    @contextlib.contextmanager
    def racing():
        with real() as conn:
            yield Racing(conn)

    web.pool.connection = racing
    try:
        with pytest.raises(Conflict, match="own_app"):
            web.close_rereview_item(item_id=ITEM, actor_id=owner, note="Closing my own app's item.", now=NOW, events_seen=0)
    finally:
        web.pool.connection = real
        web.close()
    with conn_as(pg_db, "portal_owner") as c:
        assert c.execute("SELECT closed_at FROM rereview_item WHERE id = %s", (ITEM,)).fetchone() == (None,)


# ---- more cases ----

def test_an_event_can_t_join_an_item_while_it_is_being_closed(pg_db):
    """The guard reads the item FOR SHARE: an insert waits for a close in flight, and fails once it commits."""
    import threading
    import time
    admin = person(pg_db, "a")
    seed_history(pg_db)  # an open scorecard item with its first event (a), and the event cache
    with conn_as(pg_db, "portal_collector") as c:
        c.execute("INSERT INTO rereview_event (repo, head_sha, pr, level, floor, complete, created, tree_sha, scorecard_version, signals, "
                  "run_repo, run_id, observed_at) VALUES ('acme/notes', %s, 8, 'elevated', 'standard', true, now(), %s, '0.5.1', '[]', "
                  "'acme/scorecard-factory', 2, now())", ("d" * 40, "b" * 40))
    closing = psycopg.connect(pg_db("portal_web"))  # one open transaction
    try:
        closing.execute("UPDATE rereview_item SET closed_at = now(), close_kind = 'person', closed_by = %s, close_note = 'Reviewed with the owner.' "
                        "WHERE id = %s", (admin, ITEM))
        outcome = []

        def join():
            try:
                with conn_as(pg_db, "portal_collector") as c:
                    c.execute("INSERT INTO rereview_item_event (item_id, repo, head_sha, matched, added_at) "
                              "VALUES (%s, 'acme/notes', %s, '[\"T-17\"]', now())", (ITEM, "d" * 40))
                outcome.append("inserted")
            except psycopg.Error as exc:
                outcome.append(exc)

        joiner = threading.Thread(target=join)
        joiner.start()
        time.sleep(1)
        assert joiner.is_alive() and outcome == [], "the insert waits for the close"
        closing.commit()
        joiner.join(10)
    finally:
        closing.close()
    [found] = outcome
    assert isinstance(found, errors.RaiseException) and "open" in str(found)


def racing_store(pg_db, role, after, then):
    """A PostgresStore whose connection runs `then` (on its own connection, committed) right after a statement starting with `after`."""
    import contextlib
    from portal.store.postgres import PostgresStore
    store = PostgresStore(pg_db(role))
    real = store.pool.connection

    class Racing:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, query, params=None):
            found = self.conn.execute(query, params)
            if query.startswith(after):
                then()
            return found

    @contextlib.contextmanager
    def racing():
        with real() as conn:
            yield Racing(conn)

    store.pool.connection = racing
    return store


@pytest.mark.parametrize("key, code", [("2026-10-02", "item_open"), ("2026-10-01", "item_exists")])
def test_an_item_opened_by_a_concurrent_pass_is_named_by_the_index_it_hit(pg_db, key, code):
    from portal.store.base import Conflict

    def other_pass():
        open_item(pg_db)  # review_overdue 2026-10-01, open

    store = racing_store(pg_db, "portal_collector", "SELECT 1 FROM rereview_item WHERE lower(repo) = lower(%s) AND reason = %s AND closed_at IS NULL",
                         other_pass)
    try:
        with pytest.raises(Conflict, match=code):
            store.open_rereview_item(repo="acme/notes", reason="review_overdue", key=key, now=NOW)
    finally:
        store.close()


def test_a_store_role_s_wrong_close_is_a_conflict_not_a_database_error(pg_db):
    from portal.store.base import Conflict
    from portal.store.postgres import PostgresStore
    admin = person(pg_db, "a")
    open_item(pg_db)
    web, collector = PostgresStore(pg_db("portal_web")), PostgresStore(pg_db("portal_collector"))
    try:
        with pytest.raises(Conflict, match="wrong_role"):  # only the collector closes as the platform
            web.auto_close_rereview_item(item_id=ITEM, note="The date moved, says the web.", now=NOW)
        with pytest.raises(Conflict, match="wrong_role"):  # only the web closes as a person
            collector.close_rereview_item(item_id=ITEM, actor_id=admin, note="Closed by the collector?", now=NOW, events_seen=0)
        assert web.rereview_items()[0].closed_at is None
    finally:
        web.close()
        collector.close()


def test_the_collector_role_adds_events_and_closes_date_items_through_the_store(pg_db):
    from portal.store.postgres import PostgresStore
    from test_portal_store_contract import event_doc
    collector = PostgresStore(pg_db("portal_collector"))
    try:
        for head, run_id in (("a" * 40, 1), ("d" * 40, 2)):
            collector.add_rereview_event(doc=event_doc(head=head), run_repo="acme/scorecard-factory", run_id=run_id, now=NOW)
        item = collector.open_rereview_item(repo="acme/notes", reason="scorecard_signal", key="a" * 40, now=NOW, event=("a" * 40, ("T-17",)))
        item = collector.add_rereview_item_event(item_id=item.id, head_sha="d" * 40, matched=("T-17",), now=NOW)
        assert [e.event.head_sha for e in item.events] == ["a" * 40, "d" * 40]
        due = collector.open_rereview_item(repo="acme/notes", reason="review_due_soon", key="2026-11-01", now=NOW)
        closed = collector.auto_close_rereview_item(item_id=due.id, note="The record's review date is now 2027-11-01.", now=NOW)
        assert (closed.close_kind, closed.closed_by) == ("auto", None)
    finally:
        collector.close()


def test_the_web_writes_none_of_the_collector_s_tables_and_nobody_truncates_the_caches(pg_db):
    with conn_as(pg_db, "portal_web") as c:
        for statement in ("INSERT INTO rereview_item (id, repo, reason, key, opened_at) VALUES ('i_" + "3" * 32 + "', 'acme/notes', "
                          "'review_overdue', '2026-10-01', now())",
                          "INSERT INTO rereview_event (repo, head_sha, pr, level, floor, complete, created, tree_sha, scorecard_version, "
                          "signals, run_repo, run_id, observed_at) VALUES ('acme/notes', '" + "a" * 40 + "', 7, 'elevated', 'standard', true, "
                          "now(), '" + "b" * 40 + "', '0.5.1', '[]', 'acme/scorecard-factory', 1, now())",
                          "INSERT INTO rereview_item_event (item_id, repo, head_sha, matched, added_at) VALUES ('" + ITEM + "', 'acme/notes', '"
                          + "a" * 40 + "', '[\"T-17\"]', now())"):
            with pytest.raises(errors.InsufficientPrivilege):
                c.execute(statement)
    for role in ("portal_web", "portal_collector"):
        with conn_as(pg_db, role) as c:
            for statement in ("TRUNCATE intake_observed", "TRUNCATE rereview_source"):
                with pytest.raises(errors.InsufficientPrivilege):
                    c.execute(statement)


def test_the_cache_times_are_the_database_s(pg_db):
    old = dt.datetime(2001, 1, 1, tzinfo=dt.UTC)
    with conn_as(pg_db, "portal_collector") as c:
        c.execute(OBSERVED.replace("now())", "%s)"), ("c" * 40, "p_" + "1" * 32, old))
        c.execute("INSERT INTO rereview_run (repo, run_id, outcome, seen_at) VALUES ('acme/notes', 1, 'event', %s)", (old,))
        c.execute("INSERT INTO rereview_source (source, problem, counts, checked_at) VALUES ('events', NULL, '{}', %s)", (old,))
        c.execute("INSERT INTO rereview_source (source, problem, counts, checked_at) VALUES ('events', NULL, '{}', %s) "
                  "ON CONFLICT (source) DO UPDATE SET checked_at = EXCLUDED.checked_at", (old,))
        for query in ("SELECT observed_at FROM intake_observed", "SELECT seen_at FROM rereview_run", "SELECT checked_at FROM rereview_source"):
            assert c.execute(query).fetchone()[0] > old, query


def test_the_collector_audits_a_close_only_as_the_platform_s_and_an_added_event_only_once_it_is_there(pg_db):
    admin = person(pg_db, "a")
    seed_history(pg_db)  # an open scorecard item with its event a
    audit = ("INSERT INTO audit_log (at, actor_kind, action, subject_kind, subject_id, after, reason) "
             "VALUES (now(), 'system', %s, 'rereview_item', %s, %s, 'r')")
    with conn_as(pg_db, "portal_collector") as c:
        c.execute(audit, ("rereview_item.event_added", ITEM, Jsonb({"head_sha": "a" * 40})))
        with pytest.raises(errors.RaiseException):  # no such event on the item
            c.execute(audit, ("rereview_item.event_added", ITEM, Jsonb({"head_sha": "d" * 40})))
        with pytest.raises(errors.RaiseException):  # the item is still open
            c.execute(audit, ("rereview_item.closed", ITEM, None))
    with conn_as(pg_db, "portal_web") as c:
        c.execute("UPDATE rereview_item SET closed_at = now(), close_kind = 'person', closed_by = %s, close_note = 'Reviewed with the owner.' "
                  "WHERE id = %s", (admin, ITEM))
    with conn_as(pg_db, "portal_collector") as c, pytest.raises(errors.RaiseException):  # an admin closed it, not the platform
        c.execute(audit, ("rereview_item.closed", ITEM, None))


def test_the_collector_audits_each_thing_it_did_to_an_item_once(pg_db):
    seed_history(pg_db)  # an open scorecard item with its event a
    audit = ("INSERT INTO audit_log (at, actor_kind, action, subject_kind, subject_id, after, reason) "
             "VALUES (now(), 'system', %s, 'rereview_item', %s, %s, 'r')")
    with conn_as(pg_db, "portal_collector") as c:
        for action, after in (("rereview_item.opened", None), ("rereview_item.event_added", Jsonb({"head_sha": "a" * 40}))):
            c.execute(audit, (action, ITEM, after))
            with pytest.raises(errors.RaiseException):  # a second entry for the same opening, or the same event
                c.execute(audit, (action, ITEM, after))
    due = "i_" + "4" * 32
    with conn_as(pg_db, "portal_collector") as c:
        c.execute("INSERT INTO rereview_item (id, repo, reason, key, opened_at) VALUES (%s, 'acme/notes', 'review_due_soon', '2026-11-01', now())",
                  (due,))
        c.execute("UPDATE rereview_item SET closed_at = now(), close_kind = 'auto', close_note = 'The date moved to 2027-11-01.' WHERE id = %s",
                  (due,))
        c.execute(audit, ("rereview_item.closed", due, None))
        with pytest.raises(errors.RaiseException):
            c.execute(audit, ("rereview_item.closed", due, None))


def test_only_the_collector_keeps_the_watermark_and_it_never_falls(pg_db):
    with conn_as(pg_db, "portal_collector") as c:
        c.execute("INSERT INTO rereview_watermark (repo, run_id, updated_at) VALUES ('acme/scorecard-factory', 100, now())")
        with pytest.raises(errors.RaiseException):
            c.execute("UPDATE rereview_watermark SET run_id = 50")
        with pytest.raises(errors.UniqueViolation):
            c.execute("INSERT INTO rereview_watermark (repo, run_id, updated_at) VALUES ('ACME/scorecard-factory', 200, now())")
    with conn_as(pg_db, "portal_web") as c, pytest.raises(errors.InsufficientPrivilege):
        c.execute("UPDATE rereview_watermark SET run_id = 500")
    with conn_as(pg_db, "portal_collector") as c:
        c.execute("INSERT INTO rereview_run (repo, run_id, outcome, seen_at) VALUES ('acme/notes', 9, 'untrusted_run', now())")


def test_an_event_with_a_nul_is_refused_before_the_database(pg_db):
    """a NUL in a summary would be a DataError in jsonb; the contract refuses it first, so the store says ValueError."""
    from portal.store.postgres import PostgresStore
    from test_portal_store_contract import event_doc
    collector = PostgresStore(pg_db("portal_collector"))
    try:
        signals = [{"id": "T-17", "kind": "trigger", "level": "elevated", "summary": "raised\u0000", "count": 1}]
        with pytest.raises(ValueError):
            collector.add_rereview_event(doc=event_doc(signals=signals), run_repo="acme/scorecard-factory", run_id=5, now=NOW)
        assert collector.rereview_event("acme/notes", "a" * 40) is None
    finally:
        collector.close()


def test_only_the_collector_writes_the_failed_runs(pg_db):
    with conn_as(pg_db, "portal_collector") as c:
        c.execute("INSERT INTO rereview_failed_run (repo, run_id, attempt, last_attempt_at, retention_days, seen_at) "
                  "VALUES ('acme/notes', 7, 1, now(), 90, now())")
        c.execute("UPDATE rereview_failed_run SET attempt = 2")
        with pytest.raises(errors.UniqueViolation):
            c.execute("INSERT INTO rereview_failed_run (repo, run_id, attempt, last_attempt_at, retention_days, seen_at) "
                      "VALUES ('ACME/notes', 7, 1, now(), 90, now())")
    with conn_as(pg_db, "portal_web") as c, pytest.raises(errors.InsufficientPrivilege):
        c.execute("UPDATE rereview_failed_run SET attempt = 3")   # the web reads it (the page shows the holds), never writes it
    with conn_as(pg_db, "portal_collector") as c:
        c.execute("DELETE FROM rereview_failed_run")


def test_the_web_reads_the_collector_s_progress_and_never_writes_it(pg_db):
    with conn_as(pg_db, "portal_collector") as c:
        c.execute("INSERT INTO rereview_pending_event (source, run_repo, run_id, repo, doc, seen_at) VALUES ('central', 'acme/scorecard-factory', "
                  "5, 'acme/later', '{}', now())")
        c.execute("DELETE FROM rereview_pending_event")
    with conn_as(pg_db, "portal_web") as c:
        for table in ("rereview_watermark", "rereview_failed_run", "rereview_pending_event"):
            c.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("INSERT INTO rereview_pending_event (source, run_repo, run_id, repo, doc, seen_at) VALUES ('central', 'acme/f', 6, "
                      "'acme/x', '{}', now())")


def test_the_database_refuses_a_close_while_the_record_can_t_be_read_or_by_the_last_registrant(pg_db):
    from portal.store.postgres import PostgresStore
    admin, other = person(pg_db, "a"), person(pg_db, "x")
    with conn_as(pg_db, "portal_collector") as c:
        c.execute("INSERT INTO intake_observed (repo, blob_sha, schema, by_portal, problem, observed_at) VALUES ('acme/notes', %s, NULL, false, "
                  "'invalid', now())", ("c" * 40,))
    open_item(pg_db)
    close = ("UPDATE rereview_item SET closed_at = now(), close_kind = 'person', closed_by = %s, close_note = 'Closing it, by hand.' "
             "WHERE id = %s")
    with conn_as(pg_db, "portal_web") as c, pytest.raises(errors.RaiseException, match="can't be read"):
        c.execute(close, (admin, ITEM))
    with conn_as(pg_db, "portal_collector") as c:
        c.execute("DELETE FROM intake_observed")
    store = PostgresStore(pg_db())
    try:
        req = store.create_registration(person_id=admin, draft={"schema": "registration-draft/v1", "repo": "acme/notes"}, now=NOW)
    finally:
        store.close()
    with conn_as(pg_db, "portal_owner") as c:   # approved: as far as this test is concerned, the request's state is what counts
        c.execute("ALTER TABLE request DISABLE TRIGGER USER")
        c.execute("UPDATE request SET state = 'merged' WHERE id = %s", (req.id,))
        c.execute("ALTER TABLE request ENABLE TRIGGER USER")
    with conn_as(pg_db, "portal_web") as c:
        with pytest.raises(errors.RaiseException, match="owns"):
            c.execute(close, (admin, ITEM))
        c.execute(close, (other, ITEM))
