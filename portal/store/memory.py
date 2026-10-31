"""MemoryStore: the Store in one process, for tests and local runs. A lock makes each method atomic, like a transaction."""

import collections
import dataclasses as dc
import secrets
import threading

from ..ids import new_id
from .base import (DATE_REASONS, LINK_SOURCES, OPEN_STATES, PROBLEMS, REPO_NAME, RUN_OUTCOMES, AuditEntry, Awaiting, Conflict, Evidence, ItemEvent,
                   FailedRun, Link, Person, RepoAdmin, Request, RereviewItem, RereviewStatus, Session, check_approval, check_binding,
                   check_failed_run, check_item,
                   check_matched, check_note, check_observed, check_record, check_status, check_watermark, event_row, item_event, open_key,
                   token_hash, PendingEvent, Watermark)


REGISTERED = ("approved", "writing", "pr_open", "merged")  # a registration an admin approved


class MemoryStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._people, self._by_subject = {}, {}
        self._sessions, self._login = {}, {}
        self._links, self._admins, self._requests = {}, [], {}
        self._audit: list[AuditEntry] = []
        self._evidence, self._problems = {}, []  # (request_id, submission) -> Evidence; (request_id, submission, problem, at)
        self._intake = {}  # lower(repo) -> IntakeObserved
        self._runs = {}  # (lower(repo), run_id) -> outcome
        self._events = {}  # (source, lower(repo), head_sha) -> RereviewEvent
        self._items = {}  # id -> RereviewItem (without events)
        self._item_events = []  # (item_id, source, head_sha, matched, added_at)
        self._pending = {}  # (lower(run_repo), run_id) -> PendingEvent
        self._status = {}  # source -> RereviewStatus
        self._watermarks = {}  # lower(repo) -> Watermark
        self._failed = {}  # (lower(repo), run id) -> FailedRun
        self._stuck = {}  # (lower(repo), run id) -> passes GitHub couldn't answer for it

    def _log(self, now, actor_kind, actor_id, action, subject_kind, subject_id, before, after, reason):
        self._audit.append(AuditEntry(now, actor_kind, actor_id, action, subject_kind, subject_id, before, after, reason))

    def audit_entries(self, limit=200):
        with self._lock:
            return list(reversed(self._audit))[:limit]

    def audit_for(self, subject_kind, subject_id):
        with self._lock:
            return [e for e in self._audit if e.subject_kind == subject_kind and e.subject_id == subject_id]

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

    def admin_repos_of_person(self, person_id):
        """The repo admin rows the person's active links match (by user ID where the link has one, by login where it hasn't): what
        the writer reads to re-check a binding's requester (on Postgres, through person_admin_repos, migration 0006)."""
        links = self.links_for(person_id)
        rows = self.admin_repos([l.github_user_id for l in links if l.github_user_id is not None])
        rows += self.admin_repos_by_login([l.github_login.lower() for l in links if l.github_user_id is None])
        return sorted(set(rows), key=lambda r: (r.repo.lower(), r.github_login))

    def observed_repo(self, repo):
        with self._lock:
            return next((r.repo for r in sorted(self._admins, key=lambda r: r.repo) if r.repo.lower() == str(repo).lower()), None)

    # ---- requests ----

    def create_registration(self, *, person_id, draft, now):
        repo = draft["repo"]
        with self._lock:
            key = open_key("registration", repo, draft)
            if any(open_key(r.kind, r.repo, r.draft) == key and r.state in OPEN_STATES for r in self._requests.values()):
                raise Conflict("open_request_exists")
            req = Request(new_id("r"), "registration", repo, "pending", dict(draft), person_id, now, now)
            self._requests[req.id] = req
            self._log(now, "person", person_id, "request.created", "request", req.id, None, {"state": "pending", "repo": repo},
                      "registration submitted")
            return req

    def create_binding(self, *, request_id, person_id, final, now, current=None):
        check_binding(request_id, person_id, final)
        repo = final["repo"]
        with self._lock:
            if person_id not in self._people:
                raise Conflict("unknown_person")
            if request_id in self._requests:
                raise Conflict("request_exists")
            key = open_key("binding", repo, final)  # one open request per kind, repo and environment (0007)
            if any(open_key(r.kind, r.repo, r.draft) == key and r.state in OPEN_STATES for r in self._requests.values()):
                raise Conflict("open_request_exists")
            req = Request(request_id, "binding", repo, "approved", dict(final), person_id, now, now, final=dict(final))
            self._requests[req.id] = req
            self._log(now, "person", person_id, "request.created", "request", req.id, {"binding": dict(current) if current else None},
                      {"state": "approved", "repo": repo, "final": dict(final)}, "connection change requested in the portal")
            return req

    def get_request(self, request_id):
        return self._requests.get(request_id)

    def requests_by(self, person_id):
        with self._lock:
            return sorted((r for r in self._requests.values() if r.requested_by == person_id), key=lambda r: (r.created_at, r.id), reverse=True)

    def requests_for_repo(self, repo):
        with self._lock:
            return sorted((r for r in self._requests.values() if r.repo.lower() == repo.lower()), key=lambda r: (r.created_at, r.id), reverse=True)

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
                             decision_groups=(), decision_note=None, submission=req.submission + 1, floor_override=None)
            self._requests[req.id] = new
            self._log(now, "person", person_id, "request.resubmitted", "request", req.id, {"state": "sent_back"}, {"state": "pending"},
                      "resubmitted after it was sent back")
            return new

    def decide(self, *, request_id, actor_id, actor_groups, approve, note, final, now, floor_override=None):
        with self._lock:
            req = self._need(request_id)
            if req.state != "pending":
                raise Conflict("wrong_state")
            if req.requested_by == actor_id:
                raise Conflict("self_decision")
            if approve and final is None:
                raise ValueError("an approval needs the final record")
            if approve:
                check_approval(self._evidence.get((req.id, req.submission)), final, floor_override, actor_id)
            state = "approved" if approve else "sent_back"
            new = dc.replace(req, state=state, updated_at=now, decided_by=actor_id, decided_at=now, decision_groups=tuple(actor_groups),
                             decision_note=note, final=dict(final) if approve else None, floor_override=dict(floor_override) if approve and floor_override else None)
            self._requests[req.id] = new
            self._log(now, "person", actor_id, f"request.{state}", "request", req.id, {"state": "pending"},
                      {"state": state, **({"final": dict(final)} if approve else {}),
                       **({"floor_override": dict(floor_override)} if approve and floor_override else {})}, note)
            return new

    # ---- the platform's evidence (the collector inserts it; nobody changes it) ----

    def awaiting_evidence(self, limit=50):
        with self._lock:
            failing = {(p[0], p[1]) for p in self._problems}  # requests the collector already tried and couldn't finish go last
            pending = sorted((r for r in self._requests.values() if r.kind == "registration" and r.state == "pending"
                              and (r.id, r.submission) not in self._evidence), key=lambda r: ((r.id, r.submission) in failing, r.id))
            return [Awaiting(r.id, r.repo, r.submission) for r in pending[:limit]]

    def _awaiting(self, request_id, submission):
        req = self._requests.get(request_id)
        if req is None or req.kind != "registration" or req.state != "pending" or req.submission != submission:
            raise Conflict("not_awaiting")
        return req

    def add_evidence(self, *, request_id, submission, commit, evidence, floor_tier, floor_classification, rules_commit, now,
                     record_sha=None, record=None):
        check_record(record_sha, record)
        with self._lock:
            req = self._awaiting(request_id, submission)
            if (request_id, submission) in self._evidence:
                raise Conflict("evidence_exists")
            if evidence.get("repo") != req.repo or evidence.get("commit") != commit:
                raise Conflict("not_awaiting")  # the evidence must name its request's repo and its own commit
            row = Evidence(request_id, submission, commit, dict(evidence), floor_tier, floor_classification, rules_commit, now,
                           record_sha, dict(record) if record is not None else None)
            self._evidence[(request_id, submission)] = row
            return row

    def add_evidence_problem(self, *, request_id, submission, problem, now):
        if problem not in PROBLEMS:
            raise ValueError(f"unknown evidence problem {problem!r}")
        with self._lock:
            self._awaiting(request_id, submission)
            if not any(p[:3] == (request_id, submission, problem) for p in self._problems):
                self._problems.append((request_id, submission, problem, now))

    def evidence_for(self, request_id, submission):
        return self._evidence.get((request_id, submission))

    def evidence_problem_for(self, request_id, submission):
        with self._lock:
            found = [p for p in self._problems if p[:2] == (request_id, submission)]
            return found[-1][2] if found else None

    def claim_approved(self, *, now):
        with self._lock:
            ready = sorted((r for r in self._requests.values() if r.state == "approved"), key=lambda r: (r.decided_at or r.created_at, r.id))  # a binding has no decision
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

    # ---- observed: the factory's intake records (the collector replaces them all in one go) ----

    def replace_intake_observed(self, rows, *, observed_at):
        check_observed(rows)
        with self._lock:
            self._intake = {r.repo.lower(): dc.replace(r, observed_at=observed_at) for r in rows}

    def intake_observed(self, repo=None):
        with self._lock:
            rows = sorted(self._intake.values(), key=lambda r: r.repo.lower())
        return rows if repo is None else [r for r in rows if r.repo.lower() == str(repo).lower()]

    # ---- observed: scorecard runs and their events (insert-only) ----

    def rereview_runs_seen(self, repo, run_ids):
        with self._lock:
            return {i for i in run_ids if (repo.lower(), i) in self._runs}

    def record_rereview_run(self, *, repo, run_id, outcome, now):
        if not REPO_NAME.fullmatch(repo or "") or outcome not in RUN_OUTCOMES or type(run_id) is not int or run_id < 1:
            raise ValueError("a run names its repo and has a positive id and a known outcome")
        with self._lock:
            if (repo.lower(), run_id) in self._runs:
                raise Conflict("run_seen")
            self._runs[(repo.lower(), run_id)] = outcome

    def rereview_run_counts(self):
        with self._lock:
            return dict(collections.Counter(self._runs.values()))

    def add_rereview_event(self, *, doc, run_repo, run_id, now, source="central"):
        event = event_row(doc, run_repo, run_id, now, source)
        with self._lock:
            key = (event.source, event.repo.lower(), event.head_sha)
            if key in self._events:
                return None
            self._events[key] = event
            return event

    # ---- the re-review list (authoritative) ----

    def _with_events(self, item):
        rows = [r for r in self._item_events if r[0] == item.id]
        return dc.replace(item, events=tuple(ItemEvent(self._events[(source, item.repo.lower(), sha)], matched, at)
                                             for _, source, sha, matched, at in rows))

    def _item(self, item_id):
        item = self._items.get(item_id)
        if item is None:
            raise Conflict("not_found")
        return item

    def rereview_items(self, *, open_only=True):
        with self._lock:
            found = [self._with_events(i) for i in self._items.values() if not open_only or i.closed_at is None]
        return sorted(found, key=lambda i: (i.opened_at, i.id))

    def open_rereview_item(self, *, repo, reason, key, now, event=None):
        check_item(repo, reason, key, event)
        with self._lock:
            same = [i for i in self._items.values() if i.repo.lower() == repo.lower() and i.reason == reason]
            if any(i.key == key for i in same):
                raise Conflict("item_exists")
            if any(i.closed_at is None for i in same):
                raise Conflict("item_open")
            head, matched, source = item_event(event) if event is not None else (None, None, None)
            if event is not None and (source, repo.lower(), head) not in self._events:
                raise Conflict("no_event")
            item = RereviewItem(new_id("i"), repo, reason, key, now)
            self._items[item.id] = item
            if event is not None:
                self._item_events.append((item.id, source, head, matched, now))
            self._log(now, "system", None, "rereview_item.opened", "rereview_item", item.id, None,
                      {"repo": repo, "reason": reason, "key": key}, f"the collector opened {reason} for {repo}")
            return self._with_events(item)

    def add_rereview_item_event(self, *, item_id, head_sha, matched, now, source="central"):
        check_matched(matched)
        with self._lock:
            item = self._item(item_id)
            if item.closed_at is not None:
                raise Conflict("closed")
            if item.reason != "scorecard_signal":
                raise Conflict("wrong_reason")
            if (source, item.repo.lower(), head_sha) not in self._events:
                raise Conflict("no_event")
            if any(r[0] == item_id and r[1] == source and r[2] == head_sha for r in self._item_events):
                raise Conflict("event_exists")
            self._item_events.append((item_id, source, head_sha, tuple(matched), now))
            self._log(now, "system", None, "rereview_item.event_added", "rereview_item", item_id, None,
                      {"head_sha": head_sha, "source": source, "matched": list(matched)}, "a later scorecard event matched")
            return self._with_events(item)

    def _close(self, item_id, now, kind, actor_id, note, events_seen=None):
        item = self._item(item_id)
        if item.closed_at is not None:
            raise Conflict("closed")
        if events_seen is not None and len(self._with_events(item).events) != events_seen:
            raise Conflict("item_changed")  # a later event joined after the admin looked: the decision is about what was seen (R6)
        record = self._intake.get(item.repo.lower())
        if kind == "person" and record is not None and record.problem is not None:
            raise Conflict("record_unreadable")  # who owns the app is not known
        last = next((r for r in sorted(self._requests.values(), key=lambda r: (r.created_at, r.id), reverse=True)
                     if r.kind == "registration" and r.repo.lower() == item.repo.lower() and r.state in REGISTERED), None)
        if kind == "person" and ((record is not None and record.owner == actor_id) or (last is not None and last.requested_by == actor_id)):
            raise Conflict("own_app")  # the record's owner, or the requester of the app's last approved registration
        if kind == "person" and actor_id not in self._people:
            raise Conflict("unknown_person")  # Postgres: closed_by references person
        closed = dc.replace(item, closed_at=now, close_kind=kind, closed_by=actor_id, close_note=note)
        self._items[item_id] = closed
        self._log(now, "person" if kind == "person" else "system", actor_id, "rereview_item.closed", "rereview_item", item_id,
                  {"open": True}, {"open": False, "close_kind": kind}, note)
        return self._with_events(closed)

    def close_rereview_item(self, *, item_id, actor_id, note, now, events_seen):
        check_note(note)
        if actor_id is None:
            raise Conflict("unknown_person")
        with self._lock:
            return self._close(item_id, now, "person", actor_id, note, events_seen)

    def auto_close_rereview_item(self, *, item_id, note, now):
        check_note(note)
        with self._lock:
            item = self._item(item_id)
            if item.reason not in DATE_REASONS:
                raise Conflict("wrong_reason")  # only a date closes by itself; a scorecard signal is an admin's decision
            return self._close(item_id, now, "auto", None, note)

    def set_rereview_status(self, *, source, problem, counts, now):
        check_status(source, problem, counts)
        with self._lock:
            kept = self._status.get(source)
            last_ok = now if problem is None else (kept.last_ok_at if kept else None)
            self._status[source] = RereviewStatus(source, problem, dict(counts), now, last_ok)

    def rereview_status(self):
        with self._lock:
            return dict(self._status)

    def rereview_event(self, repo, head_sha, source="central"):
        with self._lock:
            return self._events.get((source, str(repo).lower(), head_sha))

    def rereview_watermark(self, repo):
        with self._lock:
            mark = self._watermarks.get(str(repo).lower())
            return mark.run_id if mark else None

    def set_rereview_watermark(self, *, repo, run_id, now):
        check_watermark(repo, run_id)
        with self._lock:
            kept = self._watermarks.get(repo.lower())
            if kept is None or run_id >= kept.run_id:  # equal: the walk finished at the same place again, and when is noted
                self._watermarks[repo.lower()] = Watermark(kept.repo if kept else repo, run_id, now)

    def rereview_watermarks(self):
        with self._lock:
            return sorted(self._watermarks.values(), key=lambda w: w.repo.lower())

    def hold_pending_event(self, *, source, run_repo, run_id, doc, now):
        event_row(doc, run_repo, run_id, now, source)  # only a valid event waits
        with self._lock:
            self._pending.setdefault((run_repo.lower(), run_id), PendingEvent(source, run_repo, run_id, dict(doc), now))

    def pending_events(self):
        with self._lock:
            return sorted(self._pending.values(), key=lambda p: (p.run_repo.lower(), p.run_id))

    def drop_pending_event(self, *, run_repo, run_id):
        with self._lock:
            self._pending.pop((run_repo.lower(), run_id), None)

    def record_failed_run(self, *, repo, run_id, attempt, last_attempt_at, retention_days, now):
        check_failed_run(repo, run_id, attempt, retention_days)
        with self._lock:
            key = (repo.lower(), run_id)
            kept = self._failed.get(key)
            self._failed[key] = FailedRun(kept.repo if kept else repo, run_id, attempt, last_attempt_at, retention_days,
                                          kept.seen_at if kept else now, now)

    def failed_runs(self, repo=None):
        with self._lock:
            return sorted((f for (r, _), f in self._failed.items() if repo is None or r == str(repo).lower()),
                          key=lambda f: (f.repo.lower(), f.run_id))

    def note_stuck_run(self, *, repo, run_id, now):
        with self._lock:
            key = (repo.lower(), run_id)
            self._stuck[key] = self._stuck.get(key, 0) + 1
            return self._stuck[key]

    def stuck_runs(self, repo):
        with self._lock:
            return {run_id: n for (r, run_id), n in self._stuck.items() if r == str(repo).lower()}

    def clear_stuck_run(self, *, repo, run_id):
        with self._lock:
            self._stuck.pop((repo.lower(), run_id), None)

    def drop_failed_run(self, *, repo, run_id):
        with self._lock:
            self._failed.pop((repo.lower(), run_id), None)
