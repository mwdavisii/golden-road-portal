"""PostgresStore: the Store on Postgres (psycopg 3). Each method runs in one transaction (the pool's connection context commits
on success and rolls back on error), and each change writes its audit row in that same transaction."""

import secrets

from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from ..ids import new_id
from .base import LINK_SOURCES, PROBLEMS, AuditEntry, Awaiting, Conflict, Evidence, Link, LoginState, Person, RepoAdmin, Request, Session, check_approval, token_hash

AUDIT = ("INSERT INTO audit_log (at, actor_kind, actor_id, action, subject_kind, subject_id, before, after, reason) "
         "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)")


def _person(r):
    return Person(r["id"], r["issuer"], r["subject"], r["display_name"], r["email"], tuple(r["last_roles"]), r["created_at"], r["last_sign_in_at"])


def _link(r):
    return Link(r["id"], r["person_id"], r["github_login"], r["github_user_id"], r["source"], r["created_by"], r["created_at"])


def _request(r):
    return Request(id=r["id"], kind=r["kind"], repo=r["repo"], state=r["state"], draft=r["draft"], requested_by=r["requested_by"],
                   created_at=r["created_at"], updated_at=r["updated_at"], decided_by=r["decided_by"], decided_at=r["decided_at"],
                   decision_groups=tuple(r["decision_groups"] or ()), decision_note=r["decision_note"], final=r["final"],
                   branch=r["branch"], pr_number=r["pr_number"], pr_url=r["pr_url"], merge_sha=r["merge_sha"], failure_reason=r["failure_reason"],
                   head_sha=r["head_sha"], base_sha=r["base_sha"], submission=r["submission"], floor_override=r["floor_override"])


def _evidence(r):
    return Evidence(r["request_id"], r["submission"], r["commit_sha"], r["evidence"], r["floor_tier"], r["floor_classification"], r["rules_commit"],
                    r["computed_at"])


def _json(value):
    return None if value is None else Jsonb(value)


class PostgresStore:
    def __init__(self, conninfo: str, *, min_size: int = 1, max_size: int = 5):
        self.pool = ConnectionPool(conninfo, min_size=min_size, max_size=max_size,
                                   kwargs={"row_factory": dict_row, "options": "-c TimeZone=UTC"}, open=True)

    def close(self):
        self.pool.close()

    @staticmethod
    def _log(conn, now, actor_kind, actor_id, action, subject_kind, subject_id, before, after, reason):
        conn.execute(AUDIT, (now, actor_kind, actor_id, action, subject_kind, subject_id, _json(before), _json(after), reason))

    def audit_entries(self, limit=200):
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
        return [AuditEntry(r["at"], r["actor_kind"], r["actor_id"], r["action"], r["subject_kind"], r["subject_id"], r["before"], r["after"], r["reason"])
                for r in rows]

    # ---- people and sessions ----

    def sign_in(self, *, issuer, subject, display_name, email, groups, roles, now, max_age):
        roles = sorted(set(roles))
        with self.pool.connection() as conn:
            row = conn.execute("INSERT INTO person (id, issuer, subject, display_name, email, last_roles, created_at, last_sign_in_at) "
                               "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (issuer, subject) DO NOTHING RETURNING *",
                               (new_id("p"), issuer, subject, display_name, email, roles, now, now)).fetchone()
            if row is not None:
                self._log(conn, now, "system", None, "person.created", "person", row["id"], None,
                          {"display_name": display_name, "roles": roles}, "first sign-in")
            else:
                old = conn.execute("SELECT * FROM person WHERE issuer = %s AND subject = %s FOR UPDATE", (issuer, subject)).fetchone()
                row = conn.execute("UPDATE person SET display_name = %s, email = %s, last_roles = %s, last_sign_in_at = %s "
                                   "WHERE id = %s RETURNING *", (display_name, email, roles, now, old["id"])).fetchone()
                if list(old["last_roles"]) != roles:
                    self._log(conn, now, "system", None, "person.roles_changed", "person", old["id"], {"roles": list(old["last_roles"])},
                              {"roles": roles}, "groups claim at sign-in")
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            conn.execute("INSERT INTO session (token_hash, person_id, groups, roles, csrf, created_at, last_seen_at, expires_at) "
                         "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", (token_hash(token), row["id"], list(groups), roles, csrf, now, now, now + max_age))
            return _person(row), token, csrf

    def get_session(self, token, *, now, idle):
        key = token_hash(token)
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM session WHERE token_hash = %s FOR UPDATE", (key,)).fetchone()
            if row is None:
                return None
            if now >= row["expires_at"] or now - row["last_seen_at"] > idle:
                conn.execute("DELETE FROM session WHERE token_hash = %s", (key,))
                return None
            conn.execute("UPDATE session SET last_seen_at = %s WHERE token_hash = %s", (now, key))
            return Session(row["person_id"], tuple(row["groups"]), tuple(row["roles"]), row["csrf"], row["expires_at"])

    def end_session(self, token):
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM session WHERE token_hash = %s", (token_hash(token),))

    def get_person(self, person_id):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM person WHERE id = %s", (person_id,)).fetchone()
        return _person(row) if row else None

    def list_people(self):
        with self.pool.connection() as conn:
            return [_person(r) for r in conn.execute("SELECT * FROM person ORDER BY lower(display_name), id").fetchall()]

    def put_login_state(self, token, state, *, expires_at):
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO login_state (state_hash, kind, nonce, code_verifier, next_path, person_id, expires_at) "
                         "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                         (token_hash(token), state.kind, state.nonce, state.code_verifier, state.next_path, state.person_id, expires_at))

    def take_login_state(self, token, *, kind, now):
        with self.pool.connection() as conn:
            row = conn.execute("DELETE FROM login_state WHERE state_hash = %s RETURNING *", (token_hash(token),)).fetchone()
        if row is None or row["kind"] != kind or now >= row["expires_at"]:
            return None
        return LoginState(row["kind"], row["nonce"], row["code_verifier"], row["next_path"], row["person_id"])

    # ---- identity links ----

    def add_link(self, *, person_id, github_login, github_user_id, source, actor_id, reason, now):
        if source not in LINK_SOURCES:
            raise ValueError(f"unknown link source {source!r}")
        with self.pool.connection() as conn:
            if conn.execute("SELECT 1 FROM person WHERE id = %s", (person_id,)).fetchone() is None:
                raise Conflict("not_found")
            if source == "admin" and actor_id == person_id:
                raise Conflict("self_link")  # another admin must vouch for a login (the database refuses it too)
            try:
                with conn.transaction():
                    row = conn.execute("INSERT INTO identity_link (id, person_id, github_login, github_user_id, source, created_by, created_at) "
                                       "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
                                       (new_id("l"), person_id, github_login, github_user_id, source, actor_id, now)).fetchone()
            except errors.UniqueViolation:
                raise Conflict("link_taken") from None
            self._log(conn, now, "person", actor_id, "identity_link.created", "person", person_id, None,
                      {"link": row["id"], "github_login": github_login, "source": source}, reason)
            return _link(row)

    def remove_link(self, *, link_id, actor_id, reason, now):
        with self.pool.connection() as conn:
            row = conn.execute("UPDATE identity_link SET removed_at = %s, removed_by = %s WHERE id = %s AND removed_at IS NULL RETURNING *",
                               (now, actor_id, link_id)).fetchone()
            if row is None:
                raise Conflict("not_found")
            self._log(conn, now, "person", actor_id, "identity_link.removed", "person", row["person_id"],
                      {"link": row["id"], "github_login": row["github_login"], "source": row["source"]}, None, reason)
            return _link(row)

    def links_for(self, person_id):
        with self.pool.connection() as conn:
            return [_link(r) for r in conn.execute("SELECT * FROM identity_link WHERE person_id = %s AND removed_at IS NULL "
                                                   "ORDER BY created_at, id", (person_id,)).fetchall()]

    def active_links(self):
        with self.pool.connection() as conn:
            return [_link(r) for r in conn.execute("SELECT * FROM identity_link WHERE removed_at IS NULL ORDER BY created_at, id").fetchall()]

    # ---- observed ----

    def replace_repo_admins(self, rows, *, observed_at):
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM repo_admin")
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO repo_admin (repo, github_login, github_user_id, source, observed_at) VALUES (%s, %s, %s, 'github', %s)",
                                [(r.repo, r.github_login, r.github_user_id, observed_at) for r in rows])

    def admin_repos(self, github_user_ids):
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT * FROM repo_admin WHERE github_user_id = ANY(%s) ORDER BY lower(repo), github_login",
                                (list(github_user_ids),)).fetchall()
        return [RepoAdmin(r["repo"], r["github_login"], r["github_user_id"], r["observed_at"]) for r in rows]

    def admin_repos_by_login(self, logins):
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT * FROM repo_admin WHERE lower(github_login) = ANY(%s) ORDER BY lower(repo), github_login",
                                ([str(x).lower() for x in logins],)).fetchall()
        return [RepoAdmin(r["repo"], r["github_login"], r["github_user_id"], r["observed_at"]) for r in rows]

    # ---- requests ----

    def create_registration(self, *, person_id, draft, now):
        with self.pool.connection() as conn:
            try:
                with conn.transaction():
                    row = conn.execute("INSERT INTO request (id, kind, repo, state, draft, requested_by, created_at, updated_at) "
                                       "VALUES (%s, 'registration', %s, 'pending', %s, %s, %s, %s) RETURNING *",
                                       (new_id("r"), draft["repo"], Jsonb(dict(draft)), person_id, now, now)).fetchone()
            except errors.UniqueViolation:
                raise Conflict("open_request_exists") from None
            self._log(conn, now, "person", person_id, "request.created", "request", row["id"], None,
                      {"state": "pending", "repo": draft["repo"]}, "registration submitted")
            return _request(row)

    def get_request(self, request_id):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM request WHERE id = %s", (request_id,)).fetchone()
        return _request(row) if row else None

    def requests_by(self, person_id):
        with self.pool.connection() as conn:
            return [_request(r) for r in conn.execute("SELECT * FROM request WHERE requested_by = %s ORDER BY created_at DESC, id DESC",
                                                      (person_id,)).fetchall()]

    def requests_in(self, states):
        with self.pool.connection() as conn:
            return [_request(r) for r in conn.execute("SELECT * FROM request WHERE state = ANY(%s) ORDER BY created_at, id",
                                                      (list(states),)).fetchall()]

    @staticmethod
    def _locked(conn, request_id):
        row = conn.execute("SELECT * FROM request WHERE id = %s FOR UPDATE", (request_id,)).fetchone()
        if row is None:
            raise Conflict("not_found")
        return row

    def resubmit(self, *, request_id, person_id, draft, now):
        with self.pool.connection() as conn:
            row = self._locked(conn, request_id)
            if row["requested_by"] != person_id:
                raise Conflict("not_found")
            if row["state"] != "sent_back":
                raise Conflict("wrong_state")
            if draft["repo"] != row["repo"]:
                raise Conflict("repo_changed")
            row = conn.execute("UPDATE request SET state = 'pending', draft = %s, updated_at = %s, decided_by = NULL, decided_at = NULL, "
                               "decision_groups = NULL, decision_note = NULL, "
                               "submission = submission + 1 WHERE id = %s RETURNING *", (Jsonb(dict(draft)), now, request_id)).fetchone()
            self._log(conn, now, "person", person_id, "request.resubmitted", "request", request_id, {"state": "sent_back"}, {"state": "pending"},
                      "resubmitted after it was sent back")
            return _request(row)

    def decide(self, *, request_id, actor_id, actor_groups, approve, note, final, now, floor_override=None):
        if approve and final is None:
            raise ValueError("an approval needs the final record")
        state = "approved" if approve else "sent_back"
        with self.pool.connection() as conn:
            row = self._locked(conn, request_id)
            if row["state"] != "pending":
                raise Conflict("wrong_state")
            if row["requested_by"] == actor_id:
                raise Conflict("self_decision")
            if approve:
                evidence = conn.execute("SELECT * FROM registration_evidence WHERE request_id = %s AND submission = %s AND problem IS NULL",
                                        (request_id, row["submission"])).fetchone()
                check_approval(_evidence(evidence) if evidence else None, final, floor_override, actor_id)
            kept = dict(floor_override) if approve and floor_override else None
            try:
                with conn.transaction():
                    row = conn.execute("UPDATE request SET state = %s, updated_at = %s, decided_by = %s, decided_at = %s, decision_groups = %s, "
                                       "decision_note = %s, final = %s, floor_override = %s WHERE id = %s RETURNING *",
                                       (state, now, actor_id, now, list(actor_groups), note, _json(dict(final) if approve else None),
                                        _json(kept), request_id)).fetchone()
            except errors.RaiseException:  # the database's own rule (migration 0005) saw what check_approval did not
                raise Conflict("below_floor") from None
            self._log(conn, now, "person", actor_id, f"request.{state}", "request", request_id, {"state": "pending"},
                      {"state": state, **({"final": dict(final)} if approve else {}), **({"floor_override": kept} if kept else {})}, note)
            return _request(row)

    # ---- the platform's evidence (the collector inserts it; nobody changes it) ----

    def awaiting_evidence(self, limit=50):
        # runs as the collector, which may read only these request columns (never the draft), so it names them; created_at isn't one
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT r.id, r.repo, r.submission FROM request r WHERE r.kind = 'registration' AND r.state = 'pending' "
                                "AND NOT EXISTS (SELECT 1 FROM registration_evidence e WHERE e.request_id = r.id AND e.submission = r.submission "
                                "AND e.problem IS NULL) "
                                "ORDER BY EXISTS (SELECT 1 FROM registration_evidence p WHERE p.request_id = r.id AND p.submission = r.submission "
                                "AND p.problem IS NOT NULL), r.id LIMIT %s", (limit,)).fetchall()
        return [Awaiting(r["id"], r["repo"], r["submission"]) for r in rows]

    def add_evidence(self, *, request_id, submission, commit, evidence, floor_tier, floor_classification, rules_commit, now):
        with self.pool.connection() as conn:
            try:
                with conn.transaction():
                    row = conn.execute("INSERT INTO registration_evidence (request_id, submission, commit_sha, evidence, floor_tier, "
                                       "floor_classification, rules_commit, computed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                                       (request_id, submission, commit, Jsonb(dict(evidence)), floor_tier, floor_classification, rules_commit,
                                        now)).fetchone()
            except errors.UniqueViolation:
                raise Conflict("evidence_exists") from None
            except errors.RaiseException:
                raise Conflict("not_awaiting") from None
            return _evidence(row)

    def add_evidence_problem(self, *, request_id, submission, problem, now):
        if problem not in PROBLEMS:
            raise ValueError(f"unknown evidence problem {problem!r}")
        with self.pool.connection() as conn:
            try:
                with conn.transaction():
                    conn.execute("INSERT INTO registration_evidence (request_id, submission, problem, computed_at) VALUES (%s, %s, %s, %s) "
                                 "ON CONFLICT DO NOTHING", (request_id, submission, problem, now))
            except errors.RaiseException:
                raise Conflict("not_awaiting") from None

    def evidence_for(self, request_id, submission):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM registration_evidence WHERE request_id = %s AND submission = %s AND problem IS NULL",
                               (request_id, submission)).fetchone()
        return _evidence(row) if row else None

    def evidence_problem_for(self, request_id, submission):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT problem FROM registration_evidence WHERE request_id = %s AND submission = %s AND problem IS NOT NULL "
                               "ORDER BY computed_at DESC, problem DESC LIMIT 1", (request_id, submission)).fetchone()
        return row["problem"] if row else None

    def claim_approved(self, *, now):
        with self.pool.connection() as conn:
            row = conn.execute("UPDATE request SET state = 'writing', updated_at = %s WHERE id = (SELECT id FROM request WHERE state = 'approved' "
                               "ORDER BY decided_at, id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *", (now,)).fetchone()
            if row is None:
                return None
            self._log(conn, now, "writer", None, "request.writing", "request", row["id"], {"state": "approved"}, {"state": "writing"},
                      "the writer picked up the approved request")
            return _request(row)

    def mark_pr_open(self, *, request_id, branch, pr_number, pr_url, head_sha, base_sha, now):
        with self.pool.connection() as conn:
            if self._locked(conn, request_id)["state"] != "writing":
                raise Conflict("wrong_state")
            row = conn.execute("UPDATE request SET state = 'pr_open', updated_at = %s, branch = %s, pr_number = %s, pr_url = %s, head_sha = %s, base_sha = %s "
                               "WHERE id = %s RETURNING *", (now, branch, pr_number, pr_url, head_sha, base_sha, request_id)).fetchone()
            self._log(conn, now, "writer", None, "request.pr_open", "request", request_id, {"state": "writing"},
                      {"state": "pr_open", "branch": branch, "pr_number": pr_number, "pr_url": pr_url,
                       "head_sha": head_sha, "base_sha": base_sha}, "the writer opened the pull request")
            return _request(row)

    def mark_merged(self, *, request_id, merge_sha, now):
        with self.pool.connection() as conn:
            if self._locked(conn, request_id)["state"] != "pr_open":
                raise Conflict("wrong_state")
            row = conn.execute("UPDATE request SET state = 'merged', updated_at = %s, merge_sha = %s WHERE id = %s RETURNING *",
                               (now, merge_sha, request_id)).fetchone()
            self._log(conn, now, "writer", None, "request.merged", "request", request_id, {"state": "pr_open"},
                      {"state": "merged", "merge_sha": merge_sha}, "the pull request merged")
            return _request(row)

    def mark_failed(self, *, request_id, reason, now):
        with self.pool.connection() as conn:
            before = self._locked(conn, request_id)["state"]
            if before not in ("writing", "pr_open"):
                raise Conflict("wrong_state")
            row = conn.execute("UPDATE request SET state = 'failed', updated_at = %s, failure_reason = %s WHERE id = %s RETURNING *",
                               (now, reason, request_id)).fetchone()
            self._log(conn, now, "writer", None, "request.failed", "request", request_id, {"state": before},
                      {"state": "failed", "failure_reason": reason}, reason)
            return _request(row)
