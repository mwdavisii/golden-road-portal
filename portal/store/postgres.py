"""PostgresStore: the Store on Postgres (psycopg 3). Each method runs in one transaction (the pool's connection context commits
on success and rolls back on error), and each change writes its audit row in that same transaction."""

import secrets

from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from ..ids import new_id
from .base import (DATE_REASONS, LINK_SOURCES, PROBLEMS, REPO_NAME, RUN_OUTCOMES, AuditEntry, Awaiting, Conflict, Evidence, FailedRun,
                   IntakeObserved, ItemEvent,
                   Link, LoginState, Person, RepoAdmin, Request, RereviewEvent, RereviewItem, RereviewStatus, Session, check_approval,
                   check_binding, check_failed_run, check_item, check_matched, check_note, check_observed, check_record, check_status, check_watermark, event_row,
                   item_event, token_hash, PendingEvent, Watermark)

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
                    r["computed_at"], r["record_sha"], r["record"])


def _observed(r):
    return IntakeObserved(r["repo"], r["blob_sha"], r["schema"], r["by_portal"], r["owner"], r["tier"], r["data_classification"], r["purpose"],
                          r["review_by"], r["scorecard"], r["problem"], r["observed_at"])


def _event(r):
    return RereviewEvent(r["repo"], r["head_sha"], r["pr"], r["level"], r["floor"], r["complete"], r["created"], r["tree_sha"],
                         r["scorecard_version"], tuple(r["signals"]), r["run_repo"], r["run_id"], r["observed_at"], r["source"])


def _item(r, events=()):
    return RereviewItem(r["id"], r["repo"], r["reason"], r["key"], r["opened_at"], r["closed_at"], r["close_kind"], r["closed_by"],
                        r["close_note"], tuple(events))


# What 0010's re-review triggers say, as the Conflict the store's own checks give: the triggers fire only when a check above them
# raced a concurrent change, or a process role asked for what only the other may do.
TRIGGER_CONFLICTS = (("owns the app", "own_app"), ("can't be read", "record_unreadable"), ("closed once", "closed"), ("joins only an open", "closed"),
                     ("closes an item as a person", "wrong_role"), ("closes only a date item", "wrong_role"))
ITEM_INDEXES = {"rereview_item_once": "item_exists", "rereview_item_one_open": "item_open"}


def _trigger_conflict(exc) -> Conflict:
    code = next((code for words, code in TRIGGER_CONFLICTS if words in str(exc)), None)
    if code is None:
        raise exc
    return Conflict(code)


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

    @staticmethod
    def _audit_entry(r) -> AuditEntry:
        return AuditEntry(r["at"], r["actor_kind"], r["actor_id"], r["action"], r["subject_kind"], r["subject_id"], r["before"], r["after"], r["reason"])

    def audit_entries(self, limit=200):
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
        return [self._audit_entry(r) for r in rows]

    def audit_for(self, subject_kind, subject_id):
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT at, actor_kind, actor_id, action, subject_kind, subject_id, before, after, reason FROM audit_log "
                                "WHERE subject_kind = %s AND subject_id = %s ORDER BY id",
                                (subject_kind, subject_id)).fetchall()
        return [self._audit_entry(r) for r in rows]

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

    def admin_repos_of_person(self, person_id):
        """Through person_admin_repos (migration 0006): the writer's role can't read identity_link or repo_admin themselves."""
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT * FROM person_admin_repos(%s) ORDER BY lower(repo), github_login", (str(person_id),)).fetchall()
        return [RepoAdmin(r["repo"], r["github_login"], r["github_user_id"], r["observed_at"]) for r in rows]

    def observed_repo(self, repo):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT repo FROM repo_admin WHERE lower(repo) = lower(%s) ORDER BY repo LIMIT 1", (str(repo),)).fetchone()
        return row["repo"] if row else None

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

    def create_binding(self, *, request_id, person_id, final, now, current=None):
        check_binding(request_id, person_id, final)
        with self.pool.connection() as conn:
            try:
                with conn.transaction():
                    row = conn.execute("INSERT INTO request (id, kind, repo, state, draft, final, requested_by, created_at, updated_at) "
                                       "VALUES (%s, 'binding', %s, 'approved', %s, %s, %s, %s, %s) RETURNING *",
                                       (request_id, final["repo"], Jsonb(dict(final)), Jsonb(dict(final)), person_id, now, now)).fetchone()
            except errors.UniqueViolation as exc:
                raise Conflict("request_exists" if exc.diag.constraint_name == "request_pkey" else "open_request_exists") from None
            except errors.ForeignKeyViolation:
                raise Conflict("unknown_person") from None
            self._log(conn, now, "person", person_id, "request.created", "request", row["id"], {"binding": dict(current) if current else None},
                      {"state": "approved", "repo": final["repo"], "final": dict(final)}, "connection change requested in the portal")
            return _request(row)

    def get_request(self, request_id):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM request WHERE id = %s", (request_id,)).fetchone()
        return _request(row) if row else None

    def requests_by(self, person_id):
        with self.pool.connection() as conn:
            return [_request(r) for r in conn.execute("SELECT * FROM request WHERE requested_by = %s ORDER BY created_at DESC, id DESC",
                                                      (person_id,)).fetchall()]

    def requests_for_repo(self, repo):
        with self.pool.connection() as conn:
            return [_request(r) for r in conn.execute("SELECT * FROM request WHERE lower(repo) = lower(%s) ORDER BY created_at DESC, id DESC",
                                                      (repo,)).fetchall()]

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
            except errors.RaiseException as exc:  # the database's own rule (migrations 0005, 0008) saw what check_approval did not
                raise Conflict("stale_evidence" if "retired classification level" in str(exc) else "below_floor") from None
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

    def add_evidence(self, *, request_id, submission, commit, evidence, floor_tier, floor_classification, rules_commit, now,
                     record_sha=None, record=None):
        check_record(record_sha, record)
        with self.pool.connection() as conn:
            try:
                with conn.transaction():
                    row = conn.execute("INSERT INTO registration_evidence (request_id, submission, commit_sha, evidence, floor_tier, "
                                       "floor_classification, rules_commit, computed_at, record_sha, record) "
                                       "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                                       (request_id, submission, commit, Jsonb(dict(evidence)), floor_tier, floor_classification, rules_commit,
                                        now, record_sha, _json(record))).fetchone()
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
                               "ORDER BY coalesce(decided_at, created_at), id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *", (now,)).fetchone()
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

    # ---- observed: the factory's intake records ----

    def replace_intake_observed(self, rows, *, observed_at):
        check_observed(rows)
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM intake_observed")
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO intake_observed (repo, blob_sha, schema, by_portal, owner, tier, data_classification, purpose, "
                                "review_by, scorecard, problem, observed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                                [(r.repo, r.blob_sha, r.schema, r.by_portal, r.owner, r.tier, r.data_classification, r.purpose, r.review_by,
                                  r.scorecard, r.problem, observed_at) for r in rows])

    def intake_observed(self, repo=None):
        with self.pool.connection() as conn:
            if repo is None:
                rows = conn.execute("SELECT * FROM intake_observed ORDER BY lower(repo)").fetchall()
            else:
                rows = conn.execute("SELECT * FROM intake_observed WHERE lower(repo) = lower(%s)", (str(repo),)).fetchall()
        return [_observed(r) for r in rows]

    # ---- observed: scorecard runs and their events ----

    def rereview_runs_seen(self, repo, run_ids):
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT run_id FROM rereview_run WHERE lower(repo) = lower(%s) AND run_id = ANY(%s)",
                                (repo, list(run_ids))).fetchall()
        return {r["run_id"] for r in rows}

    def record_rereview_run(self, *, repo, run_id, outcome, now):
        if not REPO_NAME.fullmatch(repo or "") or outcome not in RUN_OUTCOMES or type(run_id) is not int or run_id < 1:
            raise ValueError("a run names its repo and has a positive id and a known outcome")
        with self.pool.connection() as conn:
            try:
                with conn.transaction():
                    conn.execute("INSERT INTO rereview_run (repo, run_id, outcome, seen_at) VALUES (%s, %s, %s, %s)", (repo, run_id, outcome, now))
            except errors.UniqueViolation:
                raise Conflict("run_seen") from None

    def rereview_run_counts(self):
        with self.pool.connection() as conn:
            return {r["outcome"]: r["n"] for r in conn.execute("SELECT outcome, count(*) AS n FROM rereview_run GROUP BY outcome").fetchall()}

    def add_rereview_event(self, *, doc, run_repo, run_id, now, source="central"):
        event = event_row(doc, run_repo, run_id, now, source)
        with self.pool.connection() as conn:
            row = conn.execute("INSERT INTO rereview_event (source, repo, head_sha, pr, level, floor, complete, created, tree_sha, scorecard_version, "
                               "signals, run_repo, run_id, observed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                               "ON CONFLICT DO NOTHING RETURNING *",
                               (source, event.repo, event.head_sha, event.pr, event.level, event.floor, event.complete, event.created,
                                event.tree_sha, event.scorecard_version, Jsonb(list(event.signals)), run_repo, run_id, now)).fetchone()
        return _event(row) if row else None

    # ---- the re-review list ----

    @staticmethod
    def _items(conn, rows):
        """The item rows with their events, oldest first."""
        ids = [r["id"] for r in rows]
        joined = conn.execute("SELECT ie.item_id, ie.matched, ie.added_at, e.* FROM rereview_item_event ie JOIN rereview_event e "
                              "ON e.source = ie.source AND e.repo = ie.repo AND e.head_sha = ie.head_sha WHERE ie.item_id = ANY(%s) "
                              "ORDER BY ie.added_at, e.head_sha, e.source",
                              (ids,)).fetchall() if ids else []
        events = {}
        for r in joined:
            events.setdefault(r["item_id"], []).append(ItemEvent(_event(r), tuple(r["matched"]), r["added_at"]))
        return [_item(r, events.get(r["id"], ())) for r in rows]

    def rereview_items(self, *, open_only=True):
        with self.pool.connection() as conn:
            if open_only:
                rows = conn.execute("SELECT * FROM rereview_item WHERE closed_at IS NULL ORDER BY opened_at, id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM rereview_item ORDER BY opened_at, id").fetchall()
            return self._items(conn, rows)

    def _one_item(self, conn, item_id):
        found = self._items(conn, conn.execute("SELECT * FROM rereview_item WHERE id = %s", (item_id,)).fetchall())
        if not found:
            raise Conflict("not_found")
        return found[0]

    def open_rereview_item(self, *, repo, reason, key, now, event=None):
        check_item(repo, reason, key, event)
        item_id = new_id("i")
        with self.pool.connection() as conn:
            if conn.execute("SELECT 1 FROM rereview_item WHERE lower(repo) = lower(%s) AND reason = %s AND key = %s", (repo, reason, key)).fetchone():
                raise Conflict("item_exists")
            if conn.execute("SELECT 1 FROM rereview_item WHERE lower(repo) = lower(%s) AND reason = %s AND closed_at IS NULL", (repo, reason)).fetchone():
                raise Conflict("item_open")
            stored, (head, matched, source) = None, item_event(event) if event is not None else (None, None, None)
            if event is not None:
                stored = conn.execute("SELECT repo FROM rereview_event WHERE source = %s AND lower(repo) = lower(%s) AND head_sha = %s",
                                      (source, repo, head)).fetchone()
                if stored is None:
                    raise Conflict("no_event")
            try:
                with conn.transaction():
                    conn.execute("INSERT INTO rereview_item (id, repo, reason, key, opened_at) VALUES (%s, %s, %s, %s, %s)", (item_id, repo, reason, key, now))
                    if event is not None:
                        conn.execute("INSERT INTO rereview_item_event (item_id, source, repo, head_sha, matched, added_at) "
                                     "VALUES (%s, %s, %s, %s, %s, %s)", (item_id, source, stored["repo"], head, Jsonb(list(matched)), now))
            except errors.UniqueViolation as exc:  # a concurrent pass got there first: say which rule it hit
                raise Conflict(ITEM_INDEXES.get(exc.diag.constraint_name, "item_exists")) from None
            except errors.RaiseException as exc:
                raise _trigger_conflict(exc) from None
            self._log(conn, now, "system", None, "rereview_item.opened", "rereview_item", item_id, None,
                      {"repo": repo, "reason": reason, "key": key}, f"the collector opened {reason} for {repo}")
            return self._one_item(conn, item_id)

    def add_rereview_item_event(self, *, item_id, head_sha, matched, now, source="central"):
        check_matched(matched)
        with self.pool.connection() as conn:
            item = conn.execute("SELECT * FROM rereview_item WHERE id = %s FOR UPDATE", (item_id,)).fetchone()
            if item is None:
                raise Conflict("not_found")
            if item["closed_at"] is not None:
                raise Conflict("closed")
            if item["reason"] != "scorecard_signal":
                raise Conflict("wrong_reason")
            stored = conn.execute("SELECT repo FROM rereview_event WHERE source = %s AND lower(repo) = lower(%s) AND head_sha = %s",
                                  (source, item["repo"], head_sha)).fetchone()
            if stored is None:
                raise Conflict("no_event")
            try:
                with conn.transaction():
                    conn.execute("INSERT INTO rereview_item_event (item_id, source, repo, head_sha, matched, added_at) VALUES (%s, %s, %s, %s, %s, %s)",
                                 (item_id, source, stored["repo"], head_sha, Jsonb(list(matched)), now))
            except errors.UniqueViolation:
                raise Conflict("event_exists") from None
            except errors.RaiseException as exc:
                raise _trigger_conflict(exc) from None
            self._log(conn, now, "system", None, "rereview_item.event_added", "rereview_item", item_id, None,
                      {"head_sha": head_sha, "source": source, "matched": list(matched)}, "a later scorecard event matched")
            return self._one_item(conn, item_id)

    def _close(self, item_id, now, kind, actor_id, note, events_seen=None):
        with self.pool.connection() as conn:
            item = conn.execute("SELECT * FROM rereview_item WHERE id = %s FOR UPDATE", (item_id,)).fetchone()
            if item is None:
                raise Conflict("not_found")
            if kind == "auto" and item["reason"] not in DATE_REASONS:
                raise Conflict("wrong_reason")  # before the state, as in MemoryStore
            if item["closed_at"] is not None:
                raise Conflict("closed")
            if events_seen is not None and conn.execute("SELECT count(*) AS n FROM rereview_item_event WHERE item_id = %s",
                                                        (item_id,)).fetchone()["n"] != events_seen:
                raise Conflict("item_changed")  # a later event joined after the admin looked (the item's row lock holds it now)
            if kind == "person" and not conn.execute("SELECT has_table_privilege('request', 'SELECT') AS ok").fetchone()["ok"]:
                raise Conflict("wrong_role")  # only the web closes an item as a person (the collector can't read requests either)
            if kind == "person" and conn.execute("SELECT 1 FROM intake_observed WHERE lower(repo) = lower(%s) AND problem IS NOT NULL",
                                                 (item["repo"],)).fetchone():
                raise Conflict("record_unreadable")  # who owns the app is not known
            if kind == "person" and conn.execute(
                    "SELECT 1 FROM intake_observed WHERE lower(repo) = lower(%(repo)s) AND owner = %(actor)s UNION ALL "
                    "SELECT 1 FROM (SELECT requested_by FROM request WHERE kind = 'registration' AND lower(repo) = lower(%(repo)s) "
                    "AND state IN ('approved', 'writing', 'pr_open', 'merged') ORDER BY created_at DESC, id DESC LIMIT 1) last "
                    "WHERE last.requested_by = %(actor)s", {"repo": item["repo"], "actor": actor_id}).fetchone():
                raise Conflict("own_app")  # the record's owner, or the requester of the app's last approved registration
            try:
                with conn.transaction():
                    conn.execute("UPDATE rereview_item SET closed_at = %s, close_kind = %s, closed_by = %s, close_note = %s WHERE id = %s",
                                 (now, kind, actor_id, note, item_id))
            except errors.RaiseException as exc:  # e.g. the collector recorded the admin as the owner after the check above
                raise _trigger_conflict(exc) from None
            except errors.ForeignKeyViolation:
                raise Conflict("unknown_person") from None  # closed_by references person
            self._log(conn, now, "person" if kind == "person" else "system", actor_id, "rereview_item.closed", "rereview_item", item_id,
                      {"open": True}, {"open": False, "close_kind": kind}, note)
            return self._one_item(conn, item_id)

    def close_rereview_item(self, *, item_id, actor_id, note, now, events_seen):
        check_note(note)
        if actor_id is None:
            raise Conflict("unknown_person")
        return self._close(item_id, now, "person", actor_id, note, events_seen)

    def auto_close_rereview_item(self, *, item_id, note, now):
        check_note(note)
        return self._close(item_id, now, "auto", None, note)

    def set_rereview_status(self, *, source, problem, counts, now):
        check_status(source, problem, counts)
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO rereview_source (source, problem, counts, checked_at) VALUES (%s, %s, %s, %s) ON CONFLICT (source) "
                         "DO UPDATE SET problem = EXCLUDED.problem, counts = EXCLUDED.counts, checked_at = EXCLUDED.checked_at",
                         (source, problem, Jsonb(dict(counts)), now))

    def rereview_status(self):
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT * FROM rereview_source").fetchall()
        return {r["source"]: RereviewStatus(r["source"], r["problem"], r["counts"], r["checked_at"], r["last_ok_at"]) for r in rows}

    def rereview_event(self, repo, head_sha, source="central"):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM rereview_event WHERE source = %s AND lower(repo) = lower(%s) AND head_sha = %s",
                               (source, str(repo), head_sha)).fetchone()
        return _event(row) if row else None

    def rereview_watermark(self, repo):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT run_id FROM rereview_watermark WHERE lower(repo) = lower(%s)", (str(repo),)).fetchone()
        return row["run_id"] if row else None

    def set_rereview_watermark(self, *, repo, run_id, now):
        check_watermark(repo, run_id)
        with self.pool.connection() as conn:
            found = conn.execute("SELECT repo, run_id FROM rereview_watermark WHERE lower(repo) = lower(%s) FOR UPDATE", (repo,)).fetchone()
            if found is None:
                conn.execute("INSERT INTO rereview_watermark (repo, run_id, updated_at) VALUES (%s, %s, %s)", (repo, run_id, now))
            elif run_id >= found["run_id"]:  # equal: the walk finished at the same place again, and when is noted
                conn.execute("UPDATE rereview_watermark SET run_id = %s, updated_at = %s WHERE repo = %s", (run_id, now, found["repo"]))

    def record_failed_run(self, *, repo, run_id, attempt, last_attempt_at, retention_days, now):
        check_failed_run(repo, run_id, attempt, retention_days)
        with self.pool.connection() as conn:
            found = conn.execute("SELECT repo FROM rereview_failed_run WHERE lower(repo) = lower(%s) AND run_id = %s FOR UPDATE",
                                 (repo, run_id)).fetchone()
            if found is None:
                conn.execute("INSERT INTO rereview_failed_run (repo, run_id, attempt, last_attempt_at, retention_days, seen_at, checked_at) "
                             "VALUES (%s, %s, %s, %s, %s, %s, %s)", (repo, run_id, attempt, last_attempt_at, retention_days, now, now))
            else:
                conn.execute("UPDATE rereview_failed_run SET attempt = %s, last_attempt_at = %s, retention_days = %s, checked_at = %s "
                             "WHERE repo = %s AND run_id = %s", (attempt, last_attempt_at, retention_days, now, found["repo"], run_id))

    def failed_runs(self, repo=None):
        with self.pool.connection() as conn:
            if repo is None:
                rows = conn.execute("SELECT * FROM rereview_failed_run ORDER BY lower(repo), run_id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM rereview_failed_run WHERE lower(repo) = lower(%s) ORDER BY run_id", (str(repo),)).fetchall()
        return [FailedRun(r["repo"], r["run_id"], r["attempt"], r["last_attempt_at"], r["retention_days"], r["seen_at"], r["checked_at"])
                for r in rows]

    def note_stuck_run(self, *, repo, run_id, now):
        with self.pool.connection() as conn:
            row = conn.execute("INSERT INTO rereview_stuck_run (repo, run_id, passes, first_at) VALUES (%s, %s, 1, %s) "
                               "ON CONFLICT (lower(repo), run_id) DO UPDATE SET passes = rereview_stuck_run.passes + 1 RETURNING passes",
                               (repo, run_id, now)).fetchone()
        return row["passes"]

    def stuck_runs(self, repo):
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT run_id, passes FROM rereview_stuck_run WHERE lower(repo) = lower(%s)", (str(repo),)).fetchall()
        return {r["run_id"]: r["passes"] for r in rows}

    def clear_stuck_run(self, *, repo, run_id):
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM rereview_stuck_run WHERE lower(repo) = lower(%s) AND run_id = %s", (repo, run_id))

    def drop_failed_run(self, *, repo, run_id):
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM rereview_failed_run WHERE lower(repo) = lower(%s) AND run_id = %s", (repo, run_id))

    def rereview_watermarks(self):
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT repo, run_id, updated_at FROM rereview_watermark ORDER BY lower(repo)").fetchall()
        return [Watermark(r["repo"], r["run_id"], r["updated_at"]) for r in rows]

    def hold_pending_event(self, *, source, run_repo, run_id, doc, now):
        event = event_row(doc, run_repo, run_id, now, source)  # only a valid event waits
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO rereview_pending_event (source, run_repo, run_id, repo, doc, seen_at) VALUES (%s, %s, %s, %s, %s, %s) "
                         "ON CONFLICT DO NOTHING", (source, run_repo, run_id, event.repo, Jsonb(dict(doc)), now))

    def pending_events(self):
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT * FROM rereview_pending_event ORDER BY lower(run_repo), run_id").fetchall()
        return [PendingEvent(r["source"], r["run_repo"], r["run_id"], r["doc"], r["seen_at"]) for r in rows]

    def drop_pending_event(self, *, run_repo, run_id):
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM rereview_pending_event WHERE lower(run_repo) = lower(%s) AND run_id = %s", (run_repo, run_id))
