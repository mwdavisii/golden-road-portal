import psycopg
from psycopg.types.json import Jsonb
import pytest
from portal_fakes import evidence_doc
from portal_pg import pg_db, pg_server  # noqa: F401

pytestmark = pytest.mark.docker


def test_migrations_apply_once_and_record_their_hash(pg_db):
    from portal.migrate import apply, migrations
    assert apply(pg_db()) == []  # already applied by the fixture
    with psycopg.connect(pg_db()) as conn:
        rows = conn.execute("SELECT version, sha256 FROM schema_migration ORDER BY version").fetchall()
    assert [v for v, _ in rows] == [p.stem for p in migrations()] and all(len(s) == 64 for _, s in rows)


def test_a_changed_applied_migration_stops_the_runner(pg_db):
    from portal.migrate import MigrationChanged, apply
    with psycopg.connect(pg_db()) as conn:
        conn.execute("UPDATE schema_migration SET sha256 = repeat('0', 64)")
    with pytest.raises(MigrationChanged, match="0001_core"):
        apply(pg_db())


from psycopg import errors, sql  # noqa: E402

AUDIT = ("INSERT INTO audit_log (at, actor_kind, action, subject_kind, subject_id, reason) "
         "VALUES (now(), 'system', 'test', 'person', 'p_x', 'a reason')")


def as_role(pg_db, role):
    return psycopg.connect(pg_db(role), autocommit=True)


def test_the_web_role_inserts_and_reads_audit_rows_but_never_changes_them(pg_db):
    with as_role(pg_db, "portal_web") as conn:
        conn.execute(AUDIT)
        assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 1
        for statement in ("UPDATE audit_log SET reason = 'changed'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(statement)


def test_even_the_owner_cannot_change_audit_rows(pg_db):
    with as_role(pg_db, "portal_owner") as conn:
        conn.execute(AUDIT)
        for statement in ("UPDATE audit_log SET reason = 'changed'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
            with pytest.raises(errors.RaiseException, match="append-only"):
                conn.execute(statement)


def test_the_writer_role_reads_people_and_moves_requests_only(pg_db):
    with as_role(pg_db, "portal_writer") as conn:
        conn.execute("SELECT * FROM person")
        conn.execute(AUDIT)
        for statement in ("INSERT INTO person (id, issuer, subject, display_name, created_at, last_sign_in_at) "
                          "VALUES ('p_" + "0" * 32 + "', 'i', 's', 'n', now(), now())",
                          "SELECT * FROM session", "SELECT * FROM identity_link", "INSERT INTO repo_admin VALUES ('a/b', 'x', 1, 's', now())",
                          "DELETE FROM request"):
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(statement)


def test_the_collector_role_sees_repo_admins_and_nothing_else(pg_db):
    with as_role(pg_db, "portal_collector") as conn:
        conn.execute("DELETE FROM repo_admin")
        conn.execute("INSERT INTO repo_admin VALUES ('acme/notes', 'pat', 1001, 'github', now())")
        for table in ("person", "session", "audit_log", "identity_link", "login_state"):
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(sql.Composed([sql.SQL("SELECT 1 FROM "), sql.Identifier(table)]))


def test_each_process_role_can_do_its_work_through_the_store(pg_db):
    import datetime as dt
    from portal.store.base import RepoAdmin
    from portal.store.postgres import PostgresStore
    now, day = dt.datetime(2026, 10, 23, 12, tzinfo=dt.UTC), dt.timedelta(hours=8)
    web, writer, collector = (PostgresStore(pg_db(r)) for r in ("portal_web", "portal_writer", "portal_collector"))
    try:
        owner, _, _ = web.sign_in(issuer="i", subject="o", display_name="O", email=None, groups=[], roles=[], now=now, max_age=day)
        admin, token, _ = web.sign_in(issuer="i", subject="a", display_name="A", email=None, groups=["g"], roles=["admin"], now=now, max_age=day)
        collector.replace_repo_admins([RepoAdmin("acme/notes", "o", 1, now)], observed_at=now)
        req = web.create_registration(person_id=owner.id, draft={"schema": "registration-draft/v1", "repo": "acme/notes"}, now=now)
        assert [a.request_id for a in collector.awaiting_evidence()] == [req.id]
        collector.add_evidence(request_id=req.id, submission=1, commit="e" * 40, evidence=evidence_doc("acme/notes"), floor_tier="low",
                               floor_classification="internal", rules_commit="f" * 40, now=now)
        assert collector.awaiting_evidence() == [] and web.evidence_for(req.id, 1).commit == "e" * 40
        web.decide(request_id=req.id, actor_id=admin.id, actor_groups=["g"], approve=True, note="ok", final={"schema": "intake/v2", "tier": "moderate", "data_classification": "internal", "evidence_commit": "e" * 40,
                                                                                      "floor_tier": "low", "floor_classification": "internal"}, now=now)
        assert writer.claim_approved(now=now).id == req.id
        writer.mark_pr_open(request_id=req.id, branch="b", pr_number=1, pr_url="u", head_sha="2" * 40, base_sha="1" * 40, now=now)
        writer.mark_merged(request_id=req.id, merge_sha="d" * 40, now=now)
        assert writer.get_person(owner.id).id == owner.id and web.admin_repos([1])[0].repo == "acme/notes"
    finally:
        for s in (web, writer, collector):
            s.close()


def test_tokens_are_stored_only_as_hashes(pg_db):
    import datetime as dt
    from portal.store.base import LoginState
    from portal.store.postgres import PostgresStore
    now = dt.datetime(2026, 10, 23, 12, tzinfo=dt.UTC)
    s = PostgresStore(pg_db())
    try:
        _, token, _ = s.sign_in(issuer="i", subject="o", display_name="O", email=None, groups=[], roles=[], now=now, max_age=dt.timedelta(hours=8))
        s.put_login_state("state-token", LoginState("oidc", "n", "v", "/", None), expires_at=now + dt.timedelta(minutes=10))
        with psycopg.connect(pg_db()) as conn:
            dump = str(conn.execute("SELECT * FROM session").fetchall()) + str(conn.execute("SELECT * FROM login_state").fetchall())
        assert token not in dump and "state-token" not in dump
    finally:
        s.close()


# ---- the database enforces the request state machine, no self-approval and the audit log's id and time (migration 0002) ----

import datetime as _dt  # noqa: E402

NOW = _dt.datetime(2026, 10, 23, 12, tzinfo=_dt.UTC)
COMMIT = "e" * 40
FINAL = {"schema": "intake/v2", "tier": "moderate", "data_classification": "internal", "evidence_commit": COMMIT, "floor_tier": "low",
         "floor_classification": "internal"}  # carries the evidence commit and floor that evidence_for() below stores


def evidence_for_request(store, request_id, repo, now):
    store.add_evidence(request_id=request_id, submission=1, commit=COMMIT, evidence=evidence_doc(repo, COMMIT), floor_tier="low",
                       floor_classification="internal", rules_commit="f" * 40, now=now)
DRAFT = {"schema": "registration-draft/v1", "repo": "acme/notes"}


def seed_requests(pg_db):
    """As the schema owner, through the store: a requester, an admin, and one request in each of pending, approved and writing."""
    from portal.store.postgres import PostgresStore
    store = PostgresStore(pg_db())
    try:
        day = _dt.timedelta(hours=8)
        owner, _, _ = store.sign_in(issuer="i", subject="o", display_name="O", email=None, groups=[], roles=[], now=NOW, max_age=day)
        admin, _, _ = store.sign_in(issuer="i", subject="a", display_name="A", email=None, groups=["g"], roles=["admin"], now=NOW, max_age=day)
        pending = store.create_registration(person_id=owner.id, draft={**DRAFT, "repo": "acme/pending"}, now=NOW)
        evidence_for_request(store, pending.id, "acme/pending", NOW)
        approved = store.create_registration(person_id=owner.id, draft={**DRAFT, "repo": "acme/approved"}, now=NOW)
        evidence_for_request(store, approved.id, "acme/approved", NOW)
        store.decide(request_id=approved.id, actor_id=admin.id, actor_groups=["g"], approve=True, note="ok", final=FINAL, now=NOW)
        writing = store.create_registration(person_id=owner.id, draft={**DRAFT, "repo": "acme/writing"}, now=NOW)
        evidence_for_request(store, writing.id, "acme/writing", NOW)
        store.decide(request_id=writing.id, actor_id=admin.id, actor_groups=["g"], approve=True, note="ok", final=FINAL, now=NOW)
        assert store.claim_approved(now=NOW).id in (approved.id, writing.id)
        claimed = {r.state: r.id for r in store.requests_in(("approved", "writing"))}
        return {"owner": owner.id, "admin": admin.id, "pending": pending.id, "approved": claimed["approved"], "writing": claimed["writing"]}
    finally:
        store.close()


def update(pg_db, role, statement, *params):
    with as_role(pg_db, role) as conn:
        conn.execute(statement, params)


def test_nobody_approves_their_own_request_even_by_a_direct_update(pg_db):
    ids = seed_requests(pg_db)
    with pytest.raises(errors.CheckViolation):
        update(pg_db, "portal_web", "UPDATE request SET state = 'approved', decided_by = requested_by, decided_at = now(), decision_groups = '{}', "
                                    "decision_note = 'mine', final = %s WHERE id = %s", Jsonb(FINAL), ids["pending"])  # evidence and record are in order
    with pytest.raises(errors.CheckViolation):  # nor does the owner role, which skips the transition rules' role check but not the constraint
        update(pg_db, "portal_owner", "UPDATE request SET state = 'sent_back', decided_by = requested_by, decided_at = now(), decision_groups = '{}', "
                                      "decision_note = 'mine' WHERE id = %s", ids["pending"])


def test_the_web_role_cannot_skip_to_a_writer_state_or_move_a_request_backwards(pg_db):
    ids = seed_requests(pg_db)
    for statement, key in (("UPDATE request SET state = 'merged' WHERE id = %s", "pending"),
                           ("UPDATE request SET state = 'writing' WHERE id = %s", "approved"),
                           ("UPDATE request SET state = 'pending' WHERE id = %s", "approved"),
                           ("UPDATE request SET state = 'failed' WHERE id = %s", "writing")):
        with pytest.raises(errors.RaiseException, match="illegal request transition"):
            update(pg_db, "portal_web", statement, ids[key])


def test_the_final_record_and_the_decision_cannot_be_rewritten_after_approval(pg_db):
    ids = seed_requests(pg_db)
    for role in ("portal_web", "portal_owner"):
        with pytest.raises(errors.RaiseException):
            update(pg_db, role, "UPDATE request SET final = '{\"schema\": \"evil\"}'::jsonb WHERE id = %s", ids["approved"])
        with pytest.raises(errors.RaiseException):
            update(pg_db, role, "UPDATE request SET decided_by = %s WHERE id = %s", ids["admin"], ids["approved"])
        with pytest.raises(errors.RaiseException):  # a draft change without a transition
            update(pg_db, role, "UPDATE request SET draft = '{}'::jsonb WHERE id = %s", ids["pending"])
    with pytest.raises(errors.InsufficientPrivilege):  # and the writer has no grant on those columns at all
        update(pg_db, "portal_writer", "UPDATE request SET final = '{}'::jsonb WHERE id = %s", ids["writing"])
    with pytest.raises(errors.InsufficientPrivilege):
        update(pg_db, "portal_writer", "UPDATE request SET decided_by = %s WHERE id = %s", ids["admin"], ids["writing"])


def test_the_writer_role_cannot_approve_or_restart_a_request(pg_db):
    ids = seed_requests(pg_db)
    for statement, key in (("UPDATE request SET state = 'approved' WHERE id = %s", "writing"),
                           ("UPDATE request SET state = 'approved' WHERE id = %s", "pending"),
                           ("UPDATE request SET state = 'pending' WHERE id = %s", "writing"),
                           ("UPDATE request SET state = 'merged' WHERE id = %s", "approved"),
                           ("UPDATE request SET state = 'writing' WHERE id = %s", "pending")):
        with pytest.raises(errors.RaiseException, match="illegal request transition"):
            update(pg_db, "portal_writer", statement, ids[key])
    with pytest.raises(errors.InsufficientPrivilege):
        update(pg_db, "portal_writer", "UPDATE request SET repo = 'acme/other' WHERE id = %s", ids["writing"])
    with pytest.raises(errors.RaiseException, match="branch, number and URL"):
        update(pg_db, "portal_writer", "UPDATE request SET state = 'pr_open' WHERE id = %s", ids["writing"])


def test_a_request_can_only_be_inserted_pending_and_empty(pg_db):
    ids = seed_requests(pg_db)
    insert = ("INSERT INTO request (id, kind, repo, state, draft, requested_by, created_at, updated_at@EXTRA@) "
              "VALUES ('r_" + "1" * 32 + "', 'registration', 'acme/x', %s, '{}'::jsonb, %s, now(), now()@VALUES@)")
    with pytest.raises(errors.RaiseException, match="starts pending"):
        update(pg_db, "portal_web", insert.replace("@EXTRA@", "").replace("@VALUES@", ""), "approved", ids["owner"])
    with pytest.raises(errors.RaiseException, match="starts pending"):
        update(pg_db, "portal_web", insert.replace("@EXTRA@", ", final").replace("@VALUES@", ", '{}'::jsonb"), "pending", ids["owner"])
    update(pg_db, "portal_web", insert.replace("@EXTRA@", "").replace("@VALUES@", ""), "pending", ids["owner"])


def test_the_audit_log_id_and_time_are_the_databases_whatever_the_insert_says(pg_db):
    insert = ("INSERT INTO audit_log (id, at, actor_kind, action, subject_kind, subject_id, reason) OVERRIDING SYSTEM VALUE "
              "VALUES (1, '2000-01-01T00:00:00Z', 'system', 'test', 'person', 'p_x', 'a reason') RETURNING id, at")
    with as_role(pg_db, "portal_web") as conn:
        first = conn.execute(insert).fetchone()
        second = conn.execute(insert).fetchone()  # the same explicit id would be a duplicate key if it were honored
        assert second[0] > first[0]
        assert abs((_dt.datetime.now(_dt.UTC) - first[1]).total_seconds()) < 120 and first[1].year >= 2026


# ---- migration 0003: the commit the writer opened a PR with, and no admin links a login to themselves ----

def test_the_writer_records_the_commit_it_opened_the_pr_with_and_nobody_else_can(pg_db):
    ids = seed_requests(pg_db)  # one request is in `writing`
    step = ("UPDATE request SET state = 'pr_open', branch = 'b', pr_number = 1, pr_url = 'https://github.com/a/b/pull/1'{extra} WHERE id = %s")
    with pytest.raises(errors.RaiseException, match="head and base commits"):
        update(pg_db, "portal_writer", step.format(extra=""), ids["writing"])
    with pytest.raises(errors.CheckViolation):
        update(pg_db, "portal_writer", step.format(extra=", head_sha = 'not-a-sha', base_sha = '" + "1" * 40 + "'"), ids["writing"])
    with pytest.raises(errors.InsufficientPrivilege):  # the web role has no grant on these columns
        update(pg_db, "portal_web", "UPDATE request SET head_sha = '" + "2" * 40 + "' WHERE id = %s", ids["writing"])
    update(pg_db, "portal_writer", step.format(extra=", head_sha = '" + "2" * 40 + "', base_sha = '" + "1" * 40 + "'"), ids["writing"])
    for column in ("head_sha", "base_sha"):  # frozen once the PR is open: a later step can't swap the commit the merge is pinned to
        with pytest.raises(errors.RaiseException, match="never change"):
            update(pg_db, "portal_writer", f"UPDATE request SET state = 'merged', merge_sha = '{'3' * 40}', {column} = '{'9' * 40}' WHERE id = %s", ids["writing"])
    with pytest.raises(errors.RaiseException):
        update(pg_db, "portal_owner", "UPDATE request SET head_sha = '" + "9" * 40 + "' WHERE id = %s", ids["writing"])


def test_a_request_is_inserted_without_commits(pg_db):
    ids = seed_requests(pg_db)
    insert = ("INSERT INTO request (id, kind, repo, state, draft, requested_by, created_at, updated_at, head_sha) "
              "VALUES ('r_" + "2" * 32 + "', 'registration', 'acme/y', 'pending', '{}'::jsonb, %s, now(), now(), '" + "2" * 40 + "')")
    with pytest.raises(errors.RaiseException, match="starts pending"):
        update(pg_db, "portal_web", insert, ids["owner"])


def test_an_admin_link_is_never_made_by_the_person_it_names(pg_db):
    ids = seed_requests(pg_db)
    link = ("INSERT INTO identity_link (id, person_id, github_login, source, created_by, created_at) "
            "VALUES (%s, %s, 'me-gh', %s, %s, now())")
    link_id = "l_" + "1" * 32
    with pytest.raises(errors.CheckViolation):
        with as_role(pg_db, "portal_web") as conn:
            conn.execute(link, (link_id, ids["admin"], "admin", ids["admin"]))
    with as_role(pg_db, "portal_web") as conn:  # github-oauth links are made by the person: that is the point of them
        conn.execute(link, (link_id, ids["owner"], "github-oauth", ids["owner"]))
