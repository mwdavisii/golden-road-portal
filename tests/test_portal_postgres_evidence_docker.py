"""The database's rules for the platform's evidence: insert-only, what the collector may read and write, resubmissions that count up,
and (migration 0005) no approval without evidence or below the floor without an override."""

import datetime as dt
import json

import psycopg
import pytest
from portal_fakes import evidence_doc
from portal_pg import pg_db, pg_server  # noqa: F401
from psycopg import errors
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.docker

NOW = dt.datetime(2026, 10, 23, 12, tzinfo=dt.UTC)
DRAFT = {"schema": "registration-draft/v1", "repo": "acme/notes", "purpose": "Stores short team notes.",
         "data_classification": "internal", "proposed_tier": "low"}
COMMIT = "e" * 40
EVIDENCE_SQL = ("INSERT INTO registration_evidence (request_id, submission, commit_sha, evidence, floor_tier, floor_classification, rules_commit, "
                "computed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, now())")
PROBLEM_SQL = "INSERT INTO registration_evidence (request_id, submission, problem, computed_at) VALUES (%s, %s, %s, now())"


def as_role(pg_db, role):
    return psycopg.connect(pg_db(role), autocommit=True)


def seed(pg_db, repo="acme/notes"):
    """As the schema owner, through the store: a requester, an admin and one pending registration."""
    from portal.store.postgres import PostgresStore
    store = PostgresStore(pg_db())
    try:
        day = dt.timedelta(hours=8)
        owner, _, _ = store.sign_in(issuer="i", subject="o", display_name="O", email=None, groups=[], roles=[], now=NOW, max_age=day)
        admin, _, _ = store.sign_in(issuer="i", subject="a", display_name="A", email=None, groups=["g"], roles=["admin"], now=NOW, max_age=day)
        req = store.create_registration(person_id=owner.id, draft={**DRAFT, "repo": repo}, now=NOW)
        return {"owner": owner.id, "admin": admin.id, "request": req.id, "repo": repo}
    finally:
        store.close()


def evidence_row(ids, *, submission=1, commit=COMMIT, floor=("low", "internal"), evidence=None):
    doc = evidence_doc(ids["repo"], commit) if evidence is None else evidence
    return (ids["request"], submission, commit, Jsonb(doc), floor[0], floor[1], "f" * 40)


def final_for(commit=COMMIT, floor=("low", "internal"), tier="moderate", classification="internal"):
    return {"schema": "intake/v2", "tier": tier, "data_classification": classification, "approved_at": "2026-10-23T12:00:00Z",
            "evidence_commit": commit, "floor_tier": floor[0],
            "floor_classification": floor[1]}


def decide_sql(ids, final, override=None):
    return ("UPDATE request SET state = 'approved', updated_at = now(), decided_by = %s, decided_at = now(), decision_groups = '{g}', "
            "decision_note = 'checked', final = %s, floor_override = %s WHERE id = %s",
            (ids["admin"], Jsonb(final), Jsonb(override) if override is not None else None, ids["request"]))


def test_the_collector_inserts_evidence_and_problems_and_nothing_else_new(pg_db):
    ids = seed(pg_db)
    with as_role(pg_db, "portal_collector") as conn:
        conn.execute(PROBLEM_SQL, (ids["request"], 1, "rules_missing"))
        conn.execute(EVIDENCE_SQL, evidence_row(ids))
        assert conn.execute("SELECT id, kind, repo, state, submission FROM request").fetchall() == [
            (ids["request"], "registration", "acme/notes", "pending", 1)]
        for statement in ("SELECT draft FROM request", "SELECT * FROM request", "UPDATE request SET state = 'sent_back'",
                          "INSERT INTO audit_log (at, actor_kind, action, subject_kind, subject_id, reason) VALUES (now(), 'system', 't', 'person', 'p_x', 'r')",
                          "SELECT * FROM person"):
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(statement)


@pytest.mark.parametrize("statement", ["UPDATE registration_evidence SET floor_tier = 'low'", "DELETE FROM registration_evidence",
                                       "TRUNCATE registration_evidence"])
def test_nobody_updates_or_deletes_evidence_not_even_the_owner(pg_db, statement):
    ids = seed(pg_db)
    with as_role(pg_db, "portal_collector") as conn:
        conn.execute(EVIDENCE_SQL, evidence_row(ids))
    for role in ("portal_collector", "portal_web", "portal_writer"):
        with as_role(pg_db, role) as conn, pytest.raises(errors.InsufficientPrivilege):
            conn.execute(statement)
    with as_role(pg_db, "portal_owner") as conn, pytest.raises(errors.RaiseException, match="insert-only"):
        conn.execute(statement)


def test_evidence_is_only_for_a_pending_request_s_current_submission(pg_db):
    ids = seed(pg_db)
    with as_role(pg_db, "portal_collector") as conn:
        with pytest.raises(errors.RaiseException):
            conn.execute(EVIDENCE_SQL, evidence_row(ids, submission=2))
        with pytest.raises(errors.RaiseException):
            conn.execute(PROBLEM_SQL, (ids["request"], 2, "rules_missing"))
    from portal.store.postgres import PostgresStore
    store = PostgresStore(pg_db())
    try:
        store.decide(request_id=ids["request"], actor_id=ids["admin"], actor_groups=["g"], approve=False, note="Say what it stores.", final=None, now=NOW)
    finally:
        store.close()
    with as_role(pg_db, "portal_collector") as conn, pytest.raises(errors.RaiseException):
        conn.execute(EVIDENCE_SQL, evidence_row(ids))


def test_evidence_names_its_request_s_repo_and_commit(pg_db):
    ids = seed(pg_db)
    with as_role(pg_db, "portal_collector") as conn:
        with pytest.raises(errors.RaiseException):
            conn.execute(EVIDENCE_SQL, evidence_row(ids, evidence=evidence_doc("acme/other", COMMIT)))
        with pytest.raises(errors.RaiseException):
            conn.execute(EVIDENCE_SQL, evidence_row(ids, evidence=evidence_doc(ids["repo"], "d" * 40)))


def test_an_evidence_row_or_a_problem_row_never_both(pg_db):
    ids = seed(pg_db)
    with as_role(pg_db, "portal_collector") as conn:
        with pytest.raises(errors.CheckViolation):
            conn.execute("INSERT INTO registration_evidence (request_id, submission, commit_sha, evidence, floor_tier, floor_classification, "
                         "rules_commit, problem, computed_at) VALUES (%s, 1, %s, %s, 'low', 'internal', %s, 'rules_missing', now())",
                         (ids["request"], COMMIT, Jsonb(evidence_doc(ids["repo"], COMMIT)), "f" * 40))
        with pytest.raises(errors.CheckViolation):
            conn.execute("INSERT INTO registration_evidence (request_id, submission, computed_at) VALUES (%s, 1, now())", (ids["request"],))


def test_one_evidence_row_per_submission_and_each_problem_once(pg_db):
    ids = seed(pg_db)
    with as_role(pg_db, "portal_collector") as conn:
        conn.execute(EVIDENCE_SQL, evidence_row(ids))
        with pytest.raises(errors.UniqueViolation):
            conn.execute(EVIDENCE_SQL, evidence_row(ids))
        conn.execute(PROBLEM_SQL, (ids["request"], 1, "rules_missing"))
        with pytest.raises(errors.UniqueViolation):
            conn.execute(PROBLEM_SQL, (ids["request"], 1, "rules_missing"))
        conn.execute(PROBLEM_SQL, (ids["request"], 1, "rules_invalid"))  # a different problem is a different row


def test_the_database_sets_computed_at(pg_db):
    ids = seed(pg_db)
    with as_role(pg_db, "portal_collector") as conn:
        conn.execute("INSERT INTO registration_evidence (request_id, submission, commit_sha, evidence, floor_tier, floor_classification, rules_commit, "
                     "computed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, '2000-01-01')", evidence_row(ids))
        assert conn.execute("SELECT computed_at FROM registration_evidence").fetchone()[0].year >= 2026


def sent_back(pg_db, ids):
    from portal.store.postgres import PostgresStore
    store = PostgresStore(pg_db())
    try:
        store.decide(request_id=ids["request"], actor_id=ids["admin"], actor_groups=["g"], approve=False, note="Say what it stores.", final=None, now=NOW)
    finally:
        store.close()


RESUBMIT = ("UPDATE request SET state = 'pending', updated_at = now(), decided_by = NULL, decided_at = NULL, decision_groups = NULL, "
            "decision_note = NULL, final = NULL, submission = submission + %s WHERE id = %s")


def test_a_resubmission_counts_up_and_only_the_web_can_do_it(pg_db):
    ids = seed(pg_db)
    sent_back(pg_db, ids)
    with as_role(pg_db, "portal_web") as conn:
        with pytest.raises(errors.RaiseException, match="counts up"):
            conn.execute(RESUBMIT, (0, ids["request"]))
        conn.execute(RESUBMIT, (1, ids["request"]))
        assert conn.execute("SELECT submission FROM request WHERE id = %s", (ids["request"],)).fetchone()[0] == 2
    with as_role(pg_db, "portal_writer") as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute("UPDATE request SET submission = 5")
    with as_role(pg_db, "portal_owner") as conn:
        with pytest.raises(errors.RaiseException):
            conn.execute("UPDATE request SET submission = 7")  # no transition: nothing but a resubmission changes it
        for column, value in (("submission", "2"), ("floor_override", "'{}'::jsonb")):
            with pytest.raises(errors.RaiseException):
                conn.execute(f"INSERT INTO request (id, kind, repo, state, draft, requested_by, created_at, updated_at, {column}) "
                             f"VALUES ('r_{'1' * 32}', 'registration', 'acme/two', 'pending', '{{}}', %s, now(), now(), {value})", (ids["owner"],))


def test_floor_override_is_written_only_by_the_approval_and_frozen_after(pg_db):
    ids = seed(pg_db)
    override = Jsonb({"by": ids["admin"], "at": "2026-10-23T12:00:00Z", "reason": "x" * 30})
    with as_role(pg_db, "portal_web") as conn, pytest.raises(errors.RaiseException):  # pending>sent_back with an override
        conn.execute("UPDATE request SET state = 'sent_back', updated_at = now(), decided_by = %s, decided_at = now(), decision_groups = '{g}', "
                     "decision_note = 'no', floor_override = %s WHERE id = %s", (ids["admin"], override, ids["request"]))
    sent_back(pg_db, ids)
    with as_role(pg_db, "portal_web") as conn, pytest.raises(errors.RaiseException):  # sent_back>pending with one
        conn.execute("UPDATE request SET state = 'pending', updated_at = now(), decided_by = NULL, decided_at = NULL, decision_groups = NULL, "
                     "decision_note = NULL, final = NULL, submission = submission + 1, floor_override = %s WHERE id = %s", (override, ids["request"]))
    # an approved request's override never changes on a writer transition
    ids2 = seed_approved(pg_db)
    with as_role(pg_db, "portal_owner") as conn, pytest.raises(errors.RaiseException, match="never changes"):
        conn.execute("UPDATE request SET state = 'writing', updated_at = now(), floor_override = %s WHERE id = %s", (override, ids2["request"]))
    with as_role(pg_db, "portal_writer") as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute("UPDATE request SET floor_override = %s WHERE id = %s", (override, ids2["request"]))


def seed_approved(pg_db, repo="acme/approved"):
    """A request approved with evidence at the floor, with a record that carries the stored commit and floor (it passes 0005 too)."""
    ids = seed(pg_db, repo)
    with as_role(pg_db, "portal_collector") as conn:
        conn.execute(EVIDENCE_SQL, evidence_row(ids))
    statement, params = decide_sql(ids, final_for())
    with as_role(pg_db, "portal_web") as conn:
        conn.execute(statement, params)
    return ids


def test_web_and_writer_read_evidence(pg_db):
    ids = seed(pg_db)
    with as_role(pg_db, "portal_collector") as conn:
        conn.execute(EVIDENCE_SQL, evidence_row(ids))
    for role in ("portal_web", "portal_writer"):
        with as_role(pg_db, role) as conn:
            assert conn.execute("SELECT count(*) FROM registration_evidence").fetchone()[0] == 1


# ---- migration 0005: the database refuses an approval without evidence, or below the floor without the approver's override ----


def evidenced(pg_db, floor=("low", "internal"), repo="acme/notes"):
    ids = seed(pg_db, repo)
    with as_role(pg_db, "portal_collector") as conn:
        conn.execute(EVIDENCE_SQL, evidence_row(ids, floor=floor))
    return ids


def approve_as_web(pg_db, ids, final, floor_override=None):
    statement, params = decide_sql(ids, final, floor_override)
    with as_role(pg_db, "portal_web") as conn:
        conn.execute(statement, params)


def an_override(by, reason="The spec over-declares egress; checked with the team.", at="2026-10-23T12:00:00Z"):
    return {"by": by, "at": at, "reason": reason}


def test_an_approval_without_evidence_is_refused_by_the_database(pg_db):
    ids = seed(pg_db)
    with pytest.raises(errors.RaiseException, match="needs the platform's evidence"):
        approve_as_web(pg_db, ids, final_for())


def test_an_approval_below_the_floor_without_an_override_is_refused_by_the_database(pg_db):
    ids = evidenced(pg_db, floor=("high", "internal"))
    with pytest.raises(errors.RaiseException, match="below the floor needs the approver's override"):
        approve_as_web(pg_db, ids, final_for(floor=("high", "internal"), tier="moderate"))
    ids2 = evidenced(pg_db, floor=("low", "restricted"), repo="acme/two")
    with pytest.raises(errors.RaiseException, match="below the floor needs the approver's override"):
        approve_as_web(pg_db, ids2, final_for(floor=("low", "restricted"), classification="internal"))


@pytest.mark.parametrize("who, reason", [("other", "The spec over-declares egress; checked with the team."), ("admin", "x" * 29)])
def test_an_override_must_be_the_approver_s_with_30_characters(pg_db, who, reason):
    ids = evidenced(pg_db, floor=("high", "internal"))
    override = an_override(ids["owner"] if who == "other" else ids["admin"], reason)
    final = {**final_for(floor=("high", "internal"), tier="moderate"), "floor_override": override}
    with pytest.raises(errors.RaiseException, match="override reason of at least 30"):
        approve_as_web(pg_db, ids, final, override)


@pytest.mark.parametrize("change", [{"evidence_commit": "d" * 40}, {"floor_tier": "high"}, {"floor_classification": "phi"}])
def test_the_record_must_carry_the_stored_evidence_commit_and_floor(pg_db, change):
    ids = evidenced(pg_db)
    with pytest.raises(errors.RaiseException, match="carries the stored evidence commit and floor"):
        approve_as_web(pg_db, ids, {**final_for(), **change})


def test_an_override_above_the_floor_is_refused(pg_db):
    ids = evidenced(pg_db)
    override = an_override(ids["admin"])
    with pytest.raises(errors.RaiseException, match="only for an approval below the floor"):
        approve_as_web(pg_db, ids, {**final_for(), "floor_override": override}, override)


def test_the_record_and_the_request_carry_the_same_override(pg_db):
    ids = evidenced(pg_db, floor=("high", "internal"))
    base = final_for(floor=("high", "internal"), tier="moderate")
    one, two = an_override(ids["admin"]), an_override(ids["admin"], "A different reason that is long enough too.")
    for final, row in (({**base, "floor_override": one}, two), ({**base, "floor_override": one}, None), (base, one)):
        with pytest.raises(errors.RaiseException, match="same override"):
            approve_as_web(pg_db, ids, final, row)


def test_an_approval_at_the_floor_with_evidence_is_accepted(pg_db):
    ids = evidenced(pg_db)
    approve_as_web(pg_db, ids, final_for())
    with as_role(pg_db, "portal_owner") as conn:
        assert conn.execute("SELECT state FROM request WHERE id = %s", (ids["request"],)).fetchone()[0] == "approved"


def test_an_approval_below_the_floor_with_the_approver_s_override_is_accepted(pg_db):
    ids = evidenced(pg_db, floor=("high", "internal"))
    override = an_override(ids["admin"])
    approve_as_web(pg_db, ids, {**final_for(floor=("high", "internal"), tier="moderate"), "floor_override": override}, override)
    with as_role(pg_db, "portal_owner") as conn:
        assert conn.execute("SELECT floor_override->>'by' FROM request WHERE id = %s", (ids["request"],)).fetchone()[0] == ids["admin"]


@pytest.mark.parametrize("change", [{"at": "2026-10-24T12:00:00Z"}, {"reason": "r" * 40 + "\nsecond line"}, {"reason": "r" * 1001}])
def test_the_database_wants_the_approval_time_and_a_single_line_of_at_most_1000_characters(pg_db, change):
    ids = evidenced(pg_db, floor=("high", "internal"))
    bad = {**an_override(ids["admin"]), **change}
    with pytest.raises(errors.RaiseException, match="approval time and a single line"):
        approve_as_web(pg_db, ids, {**final_for(floor=("high", "internal"), tier="moderate"), "floor_override": bad}, bad)
