"""Migration 0014 (spec 2026-11-17): the agent_definition kind, its own insert and update triggers (the writer's moves only), the
attempts it keeps, the observed agents (the collector's to write, the web's to read), the new evidence problems, and the grants."""

import datetime as dt
import uuid

import psycopg
import pytest
from psycopg import errors
from psycopg.types.json import Jsonb
from portal_pg import pg_db, pg_server  # noqa: F401
from test_portal_postgres_service_principal_docker import conn_as, person

pytestmark = pytest.mark.docker
AGENT = {"schema": "agent/v1", "display_name": "Calendar helper", "model": "chat-default", "instructions": "Help.", "starters": []}
INSERT = ("INSERT INTO request (id, kind, repo, state, draft, final, requested_by, created_at, updated_at) "
          "VALUES (%s, 'agent_definition', 'acme/notes', %s, %s, %s, %s, now(), now())")
PR = ("UPDATE request SET state = 'pr_open', branch = %s, pr_number = 1, pr_url = 'https://github.com/acme/notes/pull/1', "
      "head_sha = %s, base_sha = %s WHERE id = %s")


def rid():
    return "r_" + uuid.uuid4().hex


def agent(c, pid, draft=AGENT, state="approved", final=None):
    request_id = rid()
    c.execute(INSERT, (request_id, state, Jsonb(draft), Jsonb(draft if final is None else final), pid))
    return request_id


@pytest.mark.parametrize("draft, state, final", [
    (AGENT, "pending", None), (AGENT, "writing", None), (AGENT, "approved", {**AGENT, "model": "other"}),
    ({**AGENT, "schema": "agent/v2"}, "approved", None), ({**AGENT, "colour": "blue"}, "approved", None),
    ({k: v for k, v in AGENT.items() if k != "instructions"}, "approved", None), ({**AGENT, "display_name": ""}, "approved", None),
    ({**AGENT, "display_name": "x" * 65}, "approved", None), ({**AGENT, "display_name": "a\u2028b"}, "approved", None),
    ({**AGENT, "display_name": 7}, "approved", None), ({**AGENT, "instructions": "x" * 8001}, "approved", None),
    ({**AGENT, "instructions": "Help.\u0007"}, "approved", None), ({**AGENT, "instructions": "Help.\u0085"}, "approved", None),
    ({**AGENT, "model": "Chat"}, "approved", None), ({**AGENT, "starters": ["a", "b", "c", "d", "e"]}, "approved", None),
    ({**AGENT, "starters": "a"}, "approved", None), ({**AGENT, "starters": [""]}, "approved", None),
    ({**AGENT, "starters": ["x" * 201]}, "approved", None), ({**AGENT, "starters": ["a\nb"]}, "approved", None),
    ({**AGENT, "starters": [1]}, "approved", None), ({}, "approved", None),
])
def test_an_agent_definition_starts_approved_with_its_draft_as_its_record(pg_db, draft, state, final):
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        with pytest.raises(errors.RaiseException):
            agent(c, pid, draft, state, final)
        agent(c, pid)
        agent(c, pid, {**AGENT, "instructions": "Help.\n\tBe brief.", "starters": ["When?"]})
        agent(c, pid)  # several open at once: the writer supersedes the older


def test_it_starts_with_no_pull_request_or_attempt(pg_db):
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        for column, value in (("branch", "'portal/agent-definition'"), ("failure_reason", "'x'"), ("decision_note", "'x'")):
            with pytest.raises((errors.RaiseException, errors.CheckViolation)):
                c.execute(f"INSERT INTO request (id, kind, repo, state, draft, final, requested_by, created_at, updated_at, {column}) "
                          f"VALUES (%s, 'agent_definition', 'acme/notes', 'approved', %s, %s, %s, now(), now(), {value})",
                          (rid(), Jsonb(AGENT), Jsonb(AGENT), pid))
    with conn_as(pg_db, "portal_owner") as c:
        with pytest.raises(errors.RaiseException, match="no decision, pull request or result"):
            c.execute("INSERT INTO request (id, kind, repo, state, draft, final, requested_by, created_at, updated_at, attempts, last_error) "
                      "VALUES (%s, 'agent_definition', 'acme/notes', 'approved', %s, %s, %s, now(), now(), 1, 'x')",
                      (rid(), Jsonb(AGENT), Jsonb(AGENT), pid))


def test_only_the_writer_moves_it(pg_db):
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        request_id = agent(c, pid)
        with pytest.raises(errors.RaiseException, match="only the writer"):
            c.execute("UPDATE request SET state = 'writing' WHERE id = %s", (request_id,))
    with conn_as(pg_db, "portal_collector") as c:
        with pytest.raises((errors.RaiseException, errors.InsufficientPrivilege)):
            c.execute("UPDATE request SET state = 'writing' WHERE id = %s", (request_id,))
    with conn_as(pg_db, "portal_writer") as c:
        with pytest.raises(errors.RaiseException, match="illegal agent definition transition"):
            c.execute("UPDATE request SET updated_at = now() WHERE id = %s", (request_id,))  # approved stays approved for nobody
        c.execute("UPDATE request SET state = 'writing', updated_at = now() WHERE id = %s", (request_id,))
        c.execute("UPDATE request SET attempts = attempts + 1, last_error = 'github: 502 GET /repos/acme/notes' WHERE id = %s", (request_id,))
        with pytest.raises(errors.RaiseException, match="one more attempt"):
            c.execute("UPDATE request SET attempts = attempts + 2, last_error = 'x' WHERE id = %s", (request_id,))
        with pytest.raises(errors.RaiseException, match="recorded with one more attempt"):
            c.execute("UPDATE request SET last_error = 'y' WHERE id = %s", (request_id,))
        with pytest.raises(errors.RaiseException, match="only to count a retried failure"):
            c.execute("UPDATE request SET updated_at = now() WHERE id = %s", (request_id,))
        with pytest.raises(errors.RaiseException, match="portal/agent-definition"):
            c.execute(PR, ("other", "a" * 40, "b" * 40, request_id))
        c.execute(PR, ("portal/agent-definition", "a" * 40, "b" * 40, request_id))
        with pytest.raises((errors.RaiseException, errors.InsufficientPrivilege)):  # no grant on final, and the guard refuses it too
            c.execute("UPDATE request SET final = %s WHERE id = %s", (Jsonb({**AGENT, "model": "x"}), request_id))
        with pytest.raises(errors.RaiseException, match="recorded once"):
            c.execute("UPDATE request SET head_sha = %s, attempts = attempts + 1, last_error = 'x' WHERE id = %s", ("c" * 40, request_id))
        c.execute("UPDATE request SET attempts = attempts + 1, last_error = 'github: 503' WHERE id = %s", (request_id,))
        with pytest.raises(errors.RaiseException, match="illegal agent definition transition"):
            c.execute("UPDATE request SET state = 'approved' WHERE id = %s", (request_id,))
        with pytest.raises(errors.RaiseException, match="only a merge does"):
            c.execute("UPDATE request SET state = 'merged' WHERE id = %s", (request_id,))
        c.execute("UPDATE request SET state = 'merged', merge_sha = %s WHERE id = %s", ("c" * 40, request_id))
        with pytest.raises(errors.RaiseException, match="illegal agent definition transition"):
            c.execute("UPDATE request SET attempts = attempts + 1, last_error = 'late' WHERE id = %s", (request_id,))
        assert c.execute("SELECT attempts, last_error FROM request WHERE id = %s", (request_id,)).fetchone() == (2, "github: 503")


def test_a_superseded_one_fails_with_its_reason(pg_db):
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        request_id = agent(c, pid)
    with conn_as(pg_db, "portal_writer") as c:
        c.execute("UPDATE request SET state = 'writing' WHERE id = %s", (request_id,))
        with pytest.raises(errors.RaiseException, match="records its reason"):
            c.execute("UPDATE request SET state = 'failed' WHERE id = %s", (request_id,))
        with pytest.raises(errors.RaiseException, match="superseded only by a newer open one"):
            c.execute("UPDATE request SET state = 'failed', failure_reason = 'superseded' WHERE id = %s", (request_id,))
    with conn_as(pg_db, "portal_web") as c:
        newer = agent(c, pid)
    with conn_as(pg_db, "portal_writer") as c:
        c.execute("UPDATE request SET state = 'failed', failure_reason = 'superseded' WHERE id = %s", (request_id,))
        with pytest.raises(errors.RaiseException, match="superseded only by a newer open one"):  # the newest wins: nothing supersedes it
            c.execute("UPDATE request SET state = 'failed', failure_reason = 'superseded' WHERE id = %s", (newer,))


def test_the_newest_definition_wins_in_the_database(pg_db):
    """Review I1, as portal_writer on the database: an older definition is never claimed or opened once a newer one is open, and an
    approved one fails only as superseded."""
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        older = agent(c, pid)
        newer = agent(c, pid)
    with conn_as(pg_db, "portal_writer") as c:
        with pytest.raises(errors.RaiseException, match="a newer agent definition"):
            c.execute("UPDATE request SET state = 'writing' WHERE id = %s", (older,))
        with pytest.raises(errors.RaiseException, match="fails only as superseded"):
            c.execute("UPDATE request SET state = 'failed', failure_reason = 'pr_closed' WHERE id = %s", (older,))
        c.execute("UPDATE request SET state = 'failed', failure_reason = 'superseded' WHERE id = %s", (older,))
        c.execute("UPDATE request SET state = 'writing' WHERE id = %s", (newer,))
    with conn_as(pg_db, "portal_web") as c:
        newest = agent(c, pid)
    with conn_as(pg_db, "portal_writer") as c:
        with pytest.raises(errors.RaiseException, match="a newer agent definition"):
            c.execute(PR, ("portal/agent-definition", "a" * 40, "b" * 40, newer))
        c.execute("UPDATE request SET state = 'failed', failure_reason = 'superseded' WHERE id = %s", (newer,))
        c.execute("UPDATE request SET state = 'writing' WHERE id = %s", (newest,))
        c.execute(PR, ("portal/agent-definition", "a" * 40, "b" * 40, newest))


def test_a_definition_is_not_dated_in_the_future(pg_db):
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        with pytest.raises(errors.RaiseException, match="dated in the future"):
            c.execute("INSERT INTO request (id, kind, repo, state, draft, final, requested_by, created_at, updated_at) VALUES "
                      "(%s, 'agent_definition', 'acme/notes', 'approved', %s, %s, %s, now() + interval '1 hour', now())",
                      (rid(), Jsonb(AGENT), Jsonb(AGENT), pid))
        c.execute("INSERT INTO request (id, kind, repo, state, draft, final, requested_by, created_at, updated_at) VALUES "
                  "(%s, 'agent_definition', 'acme/notes', 'approved', %s, %s, %s, now() + interval '1 minute', now())",
                  (rid(), Jsonb(AGENT), Jsonb(AGENT), pid))  # a clock a little ahead is kept


@pytest.mark.parametrize("blank", [" ", "\t\n", "\u3000", "\u00a0\u2003", "\x1c"])
def test_blank_text_is_refused_as_agent_file_refuses_it(pg_db, blank):
    """Python's \\S is the rule (agent_file, checked first by the stores); the database's list of blank characters is the same set."""
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        with pytest.raises(errors.RaiseException):
            agent(c, pid, {**AGENT, "instructions": blank})
        with pytest.raises(errors.RaiseException):
            agent(c, pid, {**AGENT, "starters": [blank]})


def test_the_database_s_blank_characters_are_python_s():
    import re
    from pathlib import Path
    sql = (Path(__file__).resolve().parents[1] / "portal/migrations/0014_agent_definitions.sql").read_text()
    found = re.search(r"blank constant text := '\^\[(.*?)\]\*\$';", sql).group(1)
    ranges = re.findall(r"\\u([0-9a-f]{4})(?:-\\u([0-9a-f]{4}))?", found)
    chars = {chr(c) for first, last in ranges for c in range(int(first, 16), int(last or first, 16) + 1)}
    assert chars == {chr(c) for c in range(0x110000) if re.fullmatch(r"\s", chr(c))}


def test_an_observed_agent_has_the_definition_s_shape(pg_db):
    insert = "INSERT INTO agent_observed (repo, commit_sha, agent, problem, observed_at) VALUES (%s, %s, %s, NULL, now())"
    with conn_as(pg_db, "portal_collector") as c:
        for bad in ({**AGENT, "display_name": "x" * 65}, {**AGENT, "model": "Chat"}, {**AGENT, "instructions": "Help.\u0007"},
                    {**AGENT, "colour": "blue"}, {k: v for k, v in AGENT.items() if k != "model"}, {**AGENT, "starters": [1]}):
            with pytest.raises(errors.CheckViolation):
                c.execute(insert, ("acme/notes", "a" * 40, Jsonb(bad)))
        c.execute(insert, ("acme/notes", "a" * 40, Jsonb(AGENT)))


@pytest.mark.parametrize("pr_url", ["https://github.com/acme/other/pull/1", "https://github.com/acme/notes/pull/2",
                                    "https://evil.example/x/acme/notes/pull/1", "https://github.com/acmeXnotes/pull/1"])
def test_the_pull_request_is_the_request_s_repo_s(pg_db, pr_url):
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_web") as c:
        request_id = agent(c, pid)
    with conn_as(pg_db, "portal_writer") as c:
        c.execute("UPDATE request SET state = 'writing' WHERE id = %s", (request_id,))
        with pytest.raises(errors.RaiseException, match="portal/agent-definition"):
            c.execute("UPDATE request SET state = 'pr_open', branch = 'portal/agent-definition', pr_number = 1, pr_url = %s, "
                      "head_sha = %s, base_sha = %s WHERE id = %s", (pr_url, "a" * 40, "b" * 40, request_id))
        c.execute("UPDATE request SET state = 'pr_open', branch = 'portal/agent-definition', pr_number = 1, "
                  "pr_url = 'https://GITHUB.com/Acme/Notes/pull/1', head_sha = %s, base_sha = %s WHERE id = %s", ("a" * 40, "b" * 40, request_id))


@pytest.mark.parametrize("role", ["portal_web", "portal_collector"])
def test_only_the_writer_counts_a_retried_failure_through_the_store(pg_db, role):
    from portal.store.base import Conflict
    from portal.store.postgres import PostgresStore
    pid = person(pg_db, "owner")
    now = dt.datetime(2026, 11, 17, 12, tzinfo=dt.UTC)
    writer, other = PostgresStore(pg_db("portal_writer")), PostgresStore(pg_db(role))
    try:
        web = PostgresStore(pg_db("portal_web"))
        try:
            req = web.create_agent_definition(request_id=rid(), person_id=pid, repo="acme/notes", draft=AGENT, now=now)
        finally:
            web.close()
        assert writer.claim_approved(now=now).id == req.id
        with pytest.raises(Conflict) as caught:
            other.record_request_error(request_id=req.id, error="x", now=now)
        assert caught.value.code == "wrong_role" and writer.get_request(req.id).attempts == 0
    finally:
        writer.close()
        other.close()


def test_another_kind_still_can_t_keep_attempts(pg_db):
    pid = person(pg_db, "owner")
    with conn_as(pg_db, "portal_owner") as c:
        with pytest.raises(errors.CheckViolation):
            c.execute("INSERT INTO request (id, kind, repo, state, draft, requested_by, created_at, updated_at, attempts) "
                      "VALUES (%s, 'registration', 'acme/notes', 'pending', '{}', %s, now(), now(), 1)", (rid(), pid))


def test_the_writer_s_new_columns_are_the_agent_definition_s_only(pg_db):
    """0014 grants the writer attempts and last_error: on a binding they are still refused (0012's guard answers; 0014's check holds behind it)."""
    from portal.store.postgres import PostgresStore
    from test_portal_store_contract import binding_final
    pid = person(pg_db, "owner")
    web = PostgresStore(pg_db("portal_web"))
    try:
        bid = rid()
        web.create_binding(request_id=bid, person_id=pid, final=binding_final("acme/notes", pid, bid),
                           now=dt.datetime(2026, 11, 17, 12, tzinfo=dt.UTC))
    finally:
        web.close()
    with conn_as(pg_db, "portal_writer") as c:
        c.execute("UPDATE request SET state = 'writing' WHERE id = %s", (bid,))
        with pytest.raises((errors.RaiseException, errors.CheckViolation)):  # 0012's guard answers first
            c.execute("UPDATE request SET attempts = 1, last_error = 'x' WHERE id = %s", (bid,))


def test_the_observed_agents_are_the_collector_s_to_write_and_the_web_s_to_read(pg_db):
    row = ("acme/notes", "a" * 40, Jsonb(AGENT), None)
    insert = "INSERT INTO agent_observed (repo, commit_sha, agent, problem, observed_at) VALUES (%s, %s, %s, %s, now())"
    with conn_as(pg_db, "portal_collector") as c:
        c.execute(insert, row)
        c.execute("DELETE FROM agent_observed")
        c.execute(insert, row)
        with pytest.raises(errors.CheckViolation):
            c.execute(insert, ("acme/tools", "b" * 40, Jsonb(AGENT), "agent_yaml_invalid"))
        with pytest.raises(errors.CheckViolation):
            c.execute(insert, ("acme/tools", "b" * 40, None, "other"))
        with pytest.raises(errors.CheckViolation):
            c.execute(insert, ("acme/tools", "b" * 40, Jsonb({**AGENT, "schema": "agent/v2"}), None))
        with pytest.raises(errors.UniqueViolation):
            c.execute(insert, ("ACME/notes", "b" * 40, None, "agent_yaml_missing"))
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("UPDATE agent_observed SET problem = 'agent_yaml_missing'")
    with conn_as(pg_db, "portal_web") as c:
        assert c.execute("SELECT count(*) FROM agent_observed").fetchone()[0] == 1
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute(insert, ("acme/tools", "b" * 40, None, "agent_yaml_missing"))
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("DELETE FROM agent_observed")
    with conn_as(pg_db, "portal_writer") as c:
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("SELECT * FROM agent_observed")


def test_the_evidence_problems_are_widened(pg_db):
    from portal.store.postgres import PostgresStore
    pid = person(pg_db, "owner")
    now = dt.datetime(2026, 11, 17, 12, tzinfo=dt.UTC)
    web, collector = PostgresStore(pg_db("portal_web")), PostgresStore(pg_db("portal_collector"))
    try:
        reg = web.create_registration(person_id=pid, now=now, ownership_acknowledged_at=now, draft={
            "schema": "registration-draft/v1", "repo": "acme/notes", "purpose": "Stores short team notes.", "data_classification": "public"})
        collector.add_evidence_problem(request_id=reg.id, submission=1, problem="agent_yaml_missing", now=now)
        assert web.evidence_problem_for(reg.id, 1) == "agent_yaml_missing"
    finally:
        web.close()
        collector.close()


def test_each_process_role_does_its_part_through_the_store(pg_db):
    from portal.store.base import AgentObserved, Conflict
    from portal.store.postgres import PostgresStore
    pid = person(pg_db, "owner")
    now = dt.datetime(2026, 11, 17, 12, tzinfo=dt.UTC)
    web, writer, collector = (PostgresStore(pg_db(r)) for r in ("portal_web", "portal_writer", "portal_collector"))
    try:
        req = web.create_agent_definition(request_id=rid(), person_id=pid, repo="acme/notes", draft=AGENT, now=now)
        assert writer.claim_approved(now=now).id == req.id
        writer.record_request_error(request_id=req.id, error="no_agent_app: PORTAL_AGENT_APP_ID is not set", now=now)
        writer.mark_pr_open(request_id=req.id, branch="portal/agent-definition", pr_number=2, pr_url="https://github.com/acme/notes/pull/2",
                            head_sha="a" * 40, base_sha="b" * 40, now=now)
        assert writer.mark_merged(request_id=req.id, merge_sha="c" * 40, now=now).state == "merged"
        found = web.get_request(req.id)
        assert (found.attempts, found.last_error, found.final) == (1, "no_agent_app: PORTAL_AGENT_APP_ID is not set", AGENT)
        collector.replace_agent_observed([AgentObserved("acme/notes", "d" * 40, AGENT, None, now)], observed_at=now)
        assert web.agent_observed("acme/notes")[0].agent == AGENT
    finally:
        for store in (web, writer, collector):
            store.close()


def test_0014_applies_to_a_populated_database_migrated_through_0013(pg_server, monkeypatch):
    """Forward only, safe on existing rows (as 0013's own test): a registration, a binding and a service principal request survive; the
    writer still claims the binding; apply is then idempotent; an agent definition is taken and claimed."""
    from portal import migrate
    from portal.store.postgres import PostgresStore
    from portal_fakes import SP_APPROVED
    from psycopg import sql
    from test_portal_store_contract import binding_final
    now = dt.datetime(2026, 11, 17, 12, tzinfo=dt.UTC)
    every = migrate.migrations()
    name = f"portal_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server.dsn(), autocommit=True) as conn:
        conn.execute(sql.Composed([sql.SQL("CREATE DATABASE "), sql.Identifier(name), sql.SQL(" OWNER portal_owner")]))
    dsn = lambda role="portal_owner": pg_server.dsn(role, name)  # noqa: E731
    try:
        monkeypatch.setattr(migrate, "migrations", lambda: [p for p in every if p.stem[:4] <= "0013"])
        assert migrate.apply(dsn())[-1] == "0013_service_principals"
        store, web, writer = PostgresStore(dsn()), PostgresStore(dsn("portal_web")), PostgresStore(dsn("portal_writer"))
        try:
            owner, _, _ = web.sign_in(issuer="i", subject="o", display_name="O", email="o@example.test", groups=[], roles=[], now=now,
                                      max_age=dt.timedelta(hours=8))
            reg = web.create_registration(person_id=owner.id, now=now, ownership_acknowledged_at=now, draft={
                "schema": "registration-draft/v1", "repo": "acme/notes", "purpose": "Stores short team notes.", "data_classification": "public"})
            bid = "r_" + "b" * 32
            web.create_binding(request_id=bid, person_id=owner.id, final=binding_final("acme/notes", owner.id, bid), now=now)
            web.create_service_principal_request(request_id="r_" + "d" * 32, person_id=owner.id, repo="acme/notes", draft=SP_APPROVED,
                                                 state="approved", now=now)
            monkeypatch.setattr(migrate, "migrations", lambda: every)
            assert migrate.apply(dsn()) == ["0014_agent_definitions"] and migrate.apply(dsn()) == []
            assert store.get_request(reg.id).state == "pending" and len(store.service_principal_requests()) == 1
            assert writer.claim_approved(now=now).id == bid
            req = web.create_agent_definition(request_id="r_" + "e" * 32, person_id=owner.id, repo="acme/notes", draft=AGENT, now=now)
            assert writer.claim_approved(now=now).id == req.id and store.agent_observed() == []
        finally:
            for s_ in (store, web, writer):
                s_.close()
    finally:
        with psycopg.connect(pg_server.dsn(), autocommit=True) as conn:
            conn.execute(sql.Composed([sql.SQL("DROP DATABASE "), sql.Identifier(name), sql.SQL(" WITH (FORCE)")]))
