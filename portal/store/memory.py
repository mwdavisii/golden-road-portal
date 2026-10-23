"""MemoryStore: the Store in one process, for tests and local runs. A lock makes each method atomic, like a transaction."""

import dataclasses as dc
import secrets
import threading

from ..ids import new_id
from .base import LINK_SOURCES, OPEN_STATES, AuditEntry, Conflict, Link, Person, RepoAdmin, Request, Session, token_hash


class MemoryStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._people, self._by_subject = {}, {}
        self._sessions, self._login = {}, {}
        self._links, self._admins, self._requests = {}, [], {}
        self._audit: list[AuditEntry] = []

    def _log(self, now, actor_kind, actor_id, action, subject_kind, subject_id, before, after, reason):
        self._audit.append(AuditEntry(now, actor_kind, actor_id, action, subject_kind, subject_id, before, after, reason))

    def audit_entries(self, limit=200):
        with self._lock:
            return list(reversed(self._audit))[:limit]

    # ---- people and sessions ----

    def sign_in(self, *, issuer, subject, display_name, email, groups, roles, now, max_age):
        roles = tuple(sorted(set(roles)))
        with self._lock:
            pid = self._by_subject.get((issuer, subject))
            if pid is None:
                person = Person(new_id("p"), issuer, subject, display_name, email, roles, now, now)
                self._by_subject[(issuer, subject)] = person.id
                self._log(now, "system", None, "person.created", "person", person.id, None,
                          {"display_name": display_name, "roles": list(roles)}, "first sign-in")
            else:
                old = self._people[pid]
                person = dc.replace(old, display_name=display_name, email=email, last_roles=roles, last_sign_in_at=now)
                if old.last_roles != roles:
                    self._log(now, "system", None, "person.roles_changed", "person", pid, {"roles": list(old.last_roles)},
                              {"roles": list(roles)}, "groups claim at sign-in")
            self._people[person.id] = person
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self._sessions[token_hash(token)] = [Session(person.id, tuple(groups), roles, csrf, now + max_age), now]
            return person, token, csrf

    def get_session(self, token, *, now, idle):
        with self._lock:
            key = token_hash(token)
            found = self._sessions.get(key)
            if found is None:
                return None
            session, seen = found
            if now >= session.expires_at or now - seen > idle:
                del self._sessions[key]
                return None
            found[1] = now
            return session

    def end_session(self, token):
        with self._lock:
            self._sessions.pop(token_hash(token), None)

    def get_person(self, person_id):
        return self._people.get(person_id)

    def list_people(self):
        with self._lock:
            return sorted(self._people.values(), key=lambda p: (p.display_name.lower(), p.id))

    def put_login_state(self, token, state, *, expires_at):
        with self._lock:
            self._login[token_hash(token)] = (state, expires_at)

    def take_login_state(self, token, *, kind, now):
        with self._lock:
            found = self._login.pop(token_hash(token), None)
        if found is None or found[0].kind != kind or now >= found[1]:
            return None
        return found[0]

    # ---- identity links ----

    def add_link(self, *, person_id, github_login, github_user_id, source, actor_id, reason, now):
        if source not in LINK_SOURCES:
            raise ValueError(f"unknown link source {source!r}")
        with self._lock:
            if person_id not in self._people:
                raise Conflict("not_found")
            if source == "admin" and actor_id == person_id:
                raise Conflict("self_link")  # another admin must vouch for a login
            for other in self._links.values():
                if other.github_login.lower() == github_login.lower() or (github_user_id is not None and other.github_user_id == github_user_id):
                    raise Conflict("link_taken")
            link = Link(new_id("l"), person_id, github_login, github_user_id, source, actor_id, now)
            self._links[link.id] = link
            self._log(now, "person", actor_id, "identity_link.created", "person", person_id, None,
                      {"link": link.id, "github_login": github_login, "source": source}, reason)
            return link

    def remove_link(self, *, link_id, actor_id, reason, now):
        with self._lock:
            link = self._links.pop(link_id, None)
            if link is None:
                raise Conflict("not_found")
            self._log(now, "person", actor_id, "identity_link.removed", "person", link.person_id,
                      {"link": link.id, "github_login": link.github_login, "source": link.source}, None, reason)
            return link

    def links_for(self, person_id):
        with self._lock:
            return sorted((l for l in self._links.values() if l.person_id == person_id), key=lambda l: (l.created_at, l.id))

    def active_links(self):
        with self._lock:
            return sorted(self._links.values(), key=lambda l: (l.created_at, l.id))

    # ---- observed: repo admins (the collector replaces them all in one go) ----

    def replace_repo_admins(self, rows, *, observed_at):
        with self._lock:
            self._admins = [dc.replace(r, observed_at=observed_at) for r in rows]

    def admin_repos_by_login(self, logins):
        """Rows for logins (lowercase), for an admin-made link that has no GitHub user ID yet."""
        wanted = {str(x).lower() for x in logins}
        with self._lock:
            return sorted((r for r in self._admins if r.github_login.lower() in wanted), key=lambda r: (r.repo.lower(), r.github_login))

    def admin_repos(self, github_user_ids):
        ids = set(github_user_ids)
        with self._lock:
            return sorted((r for r in self._admins if r.github_user_id in ids), key=lambda r: (r.repo.lower(), r.github_login))

    # ---- requests ----

    def create_registration(self, *, person_id, draft, now):
        repo = draft["repo"]
        with self._lock:
            if any(r.kind == "registration" and r.repo.lower() == repo.lower() and r.state in OPEN_STATES for r in self._requests.values()):
                raise Conflict("open_request_exists")
            req = Request(new_id("r"), "registration", repo, "pending", dict(draft), person_id, now, now)
            self._requests[req.id] = req
            self._log(now, "person", person_id, "request.created", "request", req.id, None, {"state": "pending", "repo": repo},
                      "registration submitted")
            return req

    def get_request(self, request_id):
        return self._requests.get(request_id)

    def requests_by(self, person_id):
        with self._lock:
            return sorted((r for r in self._requests.values() if r.requested_by == person_id), key=lambda r: (r.created_at, r.id), reverse=True)

    def requests_in(self, states):
        with self._lock:
            return sorted((r for r in self._requests.values() if r.state in states), key=lambda r: (r.created_at, r.id))

    def _need(self, request_id):
        req = self._requests.get(request_id)
        if req is None:
            raise Conflict("not_found")
        return req

    def resubmit(self, *, request_id, person_id, draft, now):
        with self._lock:
            req = self._need(request_id)
            if req.requested_by != person_id:
                raise Conflict("not_found")
            if req.state != "sent_back":
                raise Conflict("wrong_state")
            if draft["repo"] != req.repo:
                raise Conflict("repo_changed")
            new = dc.replace(req, state="pending", draft=dict(draft), updated_at=now, decided_by=None, decided_at=None,
                             decision_groups=(), decision_note=None)
            self._requests[req.id] = new
            self._log(now, "person", person_id, "request.resubmitted", "request", req.id, {"state": "sent_back"}, {"state": "pending"},
                      "resubmitted after it was sent back")
            return new

    def decide(self, *, request_id, actor_id, actor_groups, approve, note, final, now):
        with self._lock:
            req = self._need(request_id)
            if req.state != "pending":
                raise Conflict("wrong_state")
            if req.requested_by == actor_id:
                raise Conflict("self_decision")
            if approve and final is None:
                raise ValueError("an approval needs the final record")
            state = "approved" if approve else "sent_back"
            new = dc.replace(req, state=state, updated_at=now, decided_by=actor_id, decided_at=now, decision_groups=tuple(actor_groups),
                             decision_note=note, final=dict(final) if approve else None)
            self._requests[req.id] = new
            self._log(now, "person", actor_id, f"request.{state}", "request", req.id, {"state": "pending"},
                      {"state": state, **({"final": dict(final)} if approve else {})}, note)
            return new

    def claim_approved(self, *, now):
        with self._lock:
            ready = sorted((r for r in self._requests.values() if r.state == "approved"), key=lambda r: (r.decided_at, r.id))
            if not ready:
                return None
            return self._move(ready[0].id, ("approved",), "writing", now, "the writer picked up the approved request")

    def _move(self, request_id, from_states, to_state, now, reason, **changes):
        req = self._need(request_id)
        if req.state not in from_states:
            raise Conflict("wrong_state")
        new = dc.replace(req, state=to_state, updated_at=now, **changes)
        self._requests[req.id] = new
        self._log(now, "writer", None, f"request.{to_state}", "request", req.id, {"state": req.state}, {"state": to_state, **changes}, reason)
        return new

    def mark_pr_open(self, *, request_id, branch, pr_number, pr_url, head_sha, base_sha, now):
        with self._lock:
            return self._move(request_id, ("writing",), "pr_open", now, "the writer opened the pull request",
                              branch=branch, pr_number=pr_number, pr_url=pr_url, head_sha=head_sha, base_sha=base_sha)

    def mark_merged(self, *, request_id, merge_sha, now):
        with self._lock:
            return self._move(request_id, ("pr_open",), "merged", now, "the pull request merged", merge_sha=merge_sha)

    def mark_failed(self, *, request_id, reason, now):
        with self._lock:
            return self._move(request_id, ("writing", "pr_open"), "failed", now, reason, failure_reason=reason)
