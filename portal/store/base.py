"""The portal's records and the Store interface. MemoryStore and PostgresStore implement it, and
tests/test_portal_store_contract.py runs the same cases against both. Every change to people, links or requests writes its
audit entry in the same transaction (spec "Data": every decision needs a reason)."""

from __future__ import annotations

import dataclasses as dc
import datetime as dt
import hashlib
import re
from typing import Protocol

OPEN_STATES = ("pending", "sent_back", "approved", "writing", "pr_open")
STATES = OPEN_STATES + ("merged", "failed")
LINK_SOURCES = ("github-oauth", "admin")


class Conflict(Exception):
    """The store refused a change. code: not_found, wrong_state, open_request_exists, link_taken, self_link, self_decision, repo_changed, request_exists, unknown_person, evidence_exists, not_awaiting, no_evidence, stale_evidence, evidence_mismatch, below_floor, run_seen, item_exists, item_open, no_event, event_exists, closed, own_app, wrong_reason, wrong_role, item_changed, record_unreadable."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


MIN_OVERRIDE, MAX_OVERRIDE = 30, 1000


def check_approval(evidence, final, floor_override, actor_id):
    """The rules an approval must meet, the same in every Store (the database enforces them again): the evidence of the request's
    current submission exists and names one of the three classification levels, the record carries its commit and floor and the very override the request does (and the scorecard mark of the record a
    change replaces), and a tier or
    classification below the floor needs the approver's own override of at least MIN_OVERRIDE characters (and none otherwise)."""
    from ..floor import CLASSIFICATIONS, below_floor
    if evidence is None:
        raise Conflict("no_evidence")
    if evidence.floor_classification not in CLASSIFICATIONS:  # computed before builder 0.5.0 (migration 0008 refuses it too)
        raise Conflict("stale_evidence")
    if (final.get("evidence_commit"), final.get("floor_tier"), final.get("floor_classification")) != (
            evidence.commit, evidence.floor_tier, evidence.floor_classification) or final.get("floor_override") != floor_override:
        raise Conflict("evidence_mismatch")
    if final.get("scorecard") != (evidence.record or {}).get("scorecard"):  # a change keeps the record's scorecard mark
        raise Conflict("evidence_mismatch")
    below = below_floor(final.get("tier"), final.get("data_classification"), evidence.floor_tier, evidence.floor_classification)
    if below:
        reason = (floor_override or {}).get("reason")
        if (not isinstance(reason, str) or not MIN_OVERRIDE <= len(reason) <= MAX_OVERRIDE or "\n" in reason or "\r" in reason
                or (floor_override or {}).get("by") != actor_id or (floor_override or {}).get("at") != final.get("approved_at")):
            raise Conflict("below_floor")
    elif floor_override is not None:
        raise Conflict("below_floor")


ENVIRONMENTS = ("test", "prod")
REQUEST_ID = re.compile(r"r_[0-9a-f]{32}")  # portal.ids.new_id("r"), as migration 0001's request_id_check


def check_binding(request_id, person_id, final):
    """A binding request's id has the portal's format, and its record is a valid binding/v2 for test or prod that names the
    request, its repo and the person. Every Store checks this before it writes anything; the database checks the naming, the
    schema and the environment again (0006, 0007). Who may change the app's connections is the web's check and the writer's
    recheck (the requester is still an admin of the repo); not the store's."""
    from ..contracts import validate
    if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
        raise ValueError("a binding request's id is not a portal request id")
    if not isinstance(final, dict) or final.get("schema") != "binding/v2" or final.get("request") != request_id \
            or final.get("linked_by") != person_id or not isinstance(final.get("repo"), str) or not final["repo"]:
        raise ValueError("a binding request's record must be a binding/v2 that names its request, its repo and its person")
    if final.get("environment") not in ENVIRONMENTS:
        raise ValueError("a binding request's record must name its environment, test or prod")
    problems = validate("binding.v2", final)
    if problems:
        raise ValueError(f"a binding request's record is not a valid binding/v2: {problems[0]}")


def open_key(kind, repo, draft):
    """What one open request is unique by, exactly migration 0007's request_one_open: its kind, its repo case-insensitively, and
    for a binding its draft's environment (test for one recorded before bindings were per environment); '' for any other kind,
    whatever its draft says."""
    if kind != "binding":
        return kind, repo.lower(), ""
    env = draft.get("environment") if isinstance(draft, dict) else None
    return kind, repo.lower(), "test" if env is None else env


def token_hash(token: str) -> bytes:
    """Sessions and login states are kept by the SHA-256 of their token, so reading the database signs nobody in."""
    return hashlib.sha256(token.encode("utf-8", "replace")).digest()


@dc.dataclass(frozen=True)
class Person:
    id: str
    issuer: str
    subject: str
    display_name: str
    email: str | None
    last_roles: tuple[str, ...]
    created_at: dt.datetime
    last_sign_in_at: dt.datetime


@dc.dataclass(frozen=True)
class Session:
    person_id: str
    groups: tuple[str, ...]
    roles: tuple[str, ...]
    csrf: str
    expires_at: dt.datetime


@dc.dataclass(frozen=True)
class LoginState:
    kind: str  # "oidc" (sign-in) or "github" (linking)
    nonce: str
    code_verifier: str
    next_path: str
    person_id: str | None


@dc.dataclass(frozen=True)
class Link:
    id: str
    person_id: str
    github_login: str
    github_user_id: int | None
    source: str
    created_by: str
    created_at: dt.datetime


@dc.dataclass(frozen=True)
class RepoAdmin:
    repo: str
    github_login: str
    github_user_id: int
    observed_at: dt.datetime


@dc.dataclass(frozen=True)
class Request:
    id: str
    kind: str
    repo: str
    state: str
    draft: dict
    requested_by: str
    created_at: dt.datetime
    updated_at: dt.datetime
    decided_by: str | None = None
    decided_at: dt.datetime | None = None
    decision_groups: tuple[str, ...] = ()
    decision_note: str | None = None
    final: dict | None = None
    branch: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    merge_sha: str | None = None
    failure_reason: str | None = None
    head_sha: str | None = None  # the commit the writer opened the pull request with: the only one it merges
    base_sha: str | None = None  # the default branch's commit it branched from
    submission: int = 1  # counts up each time a sent-back request is resubmitted; the platform's evidence is per submission
    floor_override: dict | None = None  # {by, at, reason}: the approver's written reason for approving below the floor


@dc.dataclass(frozen=True)
class Evidence:
    """What the collector read from the repo at one commit (registration-evidence/v1) and the floor computed from it."""
    request_id: str
    submission: int
    commit: str
    evidence: dict
    floor_tier: str
    floor_classification: str
    rules_commit: str
    computed_at: dt.datetime
    record_sha: str | None = None  # a change: the blob SHA of the intake record it replaces, read at rules_commit (spec 2026-10-31 C4)
    record: dict | None = None  # and what that record says (record_view), or {"schema": "invalid"}


@dc.dataclass(frozen=True)
class Awaiting:
    """A pending registration that has no evidence yet. Never carries the draft: the collector may not read it."""
    request_id: str
    repo: str
    submission: int


PROBLEMS = ("rules_missing", "rules_invalid", "rules_unreadable", "repo_unreadable", "record_unreadable", "record_case_mismatch")


@dc.dataclass(frozen=True)
class AuditEntry:
    at: dt.datetime
    actor_kind: str  # person, writer, system
    actor_id: str | None
    action: str
    subject_kind: str
    subject_id: str
    before: dict | None
    after: dict | None
    reason: str


SHA40 = re.compile(r"[0-9a-f]{40}")
REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
RECORD_SCHEMAS = ("intake/v1", "intake/v2", "invalid")


def check_record(record_sha, record) -> None:
    """A change's evidence names the record it replaces with both values, a first registration's with neither (migration 0010)."""
    if record_sha is None and record is None:
        return
    if not (isinstance(record_sha, str) and SHA40.fullmatch(record_sha) and isinstance(record, dict) and record.get("schema") in RECORD_SCHEMAS):
        raise ValueError("a change's evidence names the record it replaces by its blob SHA and its view, together")


@dc.dataclass(frozen=True)
class IntakeObserved:
    """One intake/<owner>/<repo>.yaml in the factory as the collector last read it (a cache: the file in git is the truth, P5)."""
    repo: str
    blob_sha: str
    schema: str | None  # intake/v1, intake/v2, or None when the file can't be used (problem says why)
    by_portal: bool
    owner: str | None
    tier: str | None
    data_classification: str | None
    purpose: str | None
    review_by: dt.date | None  # intake/v2 only
    scorecard: str | None  # per-app, central, or None
    problem: str | None  # invalid, unreadable, not_a_file, case_mismatch (the record under two spellings)
    observed_at: dt.datetime | None = None


INTAKE_PROBLEMS = ("invalid", "unreadable", "not_a_file", "case_mismatch")


MAX_PURPOSE = 4000


def check_observed(rows) -> None:
    """The rows the collector hands over: well-formed, one per repo in any case, with known levels and a bounded purpose. These are
    the rules migration 0010 holds intake_observed to (and a real by_portal flag), so both stores refuse the same rows."""
    from ..floor import CLASSIFICATIONS, TIERS
    seen = set()
    for r in rows:
        ok = (REPO_NAME.fullmatch(r.repo or "") and SHA40.fullmatch(r.blob_sha or "") and r.repo.lower() not in seen
              and type(r.by_portal) is bool and r.tier in (None, *TIERS) and r.data_classification in (None, *CLASSIFICATIONS)
              and (r.purpose is None or (isinstance(r.purpose, str) and len(r.purpose) <= MAX_PURPOSE))
              and (r.problem is None) == (r.schema is not None) and (r.problem is None or r.problem in INTAKE_PROBLEMS)
              and r.schema in (None, "intake/v1", "intake/v2") and (r.schema != "intake/v2" or isinstance(r.review_by, dt.date))
              and (r.schema != "intake/v1" or r.review_by is None) and r.scorecard in (None, "per-app", "central")
              and (r.problem is not None or all(isinstance(v, str) for v in (r.owner, r.tier, r.data_classification, r.purpose))))
        if not ok:
            raise ValueError(f"an observed intake row is not well-formed: {r.repo!r}")
        seen.add(r.repo.lower())


RUN_OUTCOMES = ("event", "duplicate", "invalid", "no_event", "no_artifact", "expired", "too_large", "not_registered", "repo_mismatch", "unreadable",
                "untrusted_run")  # not_registered is counted, never recorded: the run is read again on a later pass
REREVIEW_REASONS = ("review_overdue", "review_due_soon", "scorecard_signal")
DATE_REASONS = ("review_overdue", "review_due_soon")
SIGNAL_ID = re.compile(r"T-[1-9][0-9]{0,2}|builder-bumped|egress-removed|resources-lowered")
# Each collector part's status: intake and events with their own problems, every part with "failed" (it raised)
STATUS_PROBLEMS = {"repo_admins": ("failed",), "evidence": ("failed",), "intake": ("factory_unreadable", "failed"),
                   "events": ("actions_unreadable", "factory_unreadable", "factory_not_main", "artifact_host_refused", "github_busy",
                              "failed")}
SOURCES = ("central", "per-app")  # where an event came from: the factory's central review, or an app's own run (stored once per source)
MIN_NOTE, MAX_NOTE = 10, 1000
MAX_MATCHED = 40  # an event carries at most 40 signals (re-review-event/v1)


@dc.dataclass(frozen=True)
class RereviewEvent:
    """One assessed scorecard run's re-review-event/v1, validated, stored once per source, repo and head (a cache): a per-app event can
    never hide the central one of the same head."""
    repo: str
    head_sha: str
    pr: int | None
    level: str
    floor: str
    complete: bool
    created: dt.datetime
    tree_sha: str
    scorecard_version: str
    signals: tuple
    run_repo: str
    run_id: int
    observed_at: dt.datetime | None = None
    source: str = "central"


@dc.dataclass(frozen=True)
class PendingEvent:
    """A trusted run's valid event whose app has no readable intake record yet: kept, and read again each pass (a cache)."""
    source: str
    run_repo: str
    run_id: int
    doc: dict
    seen_at: dt.datetime


@dc.dataclass(frozen=True)
class Watermark:
    repo: str
    run_id: int
    updated_at: dt.datetime


@dc.dataclass(frozen=True)
class ItemEvent:
    event: RereviewEvent
    matched: tuple[str, ...]  # the signal IDs of this event that matched the configured set
    added_at: dt.datetime


@dc.dataclass(frozen=True)
class RereviewItem:
    """One app plus one reason on the re-review list (authoritative). Unique by (repo, reason, key) for ever: a closed item never reopens."""
    id: str
    repo: str
    reason: str
    key: str  # the review date (date reasons) or the first event's head SHA (scorecard_signal)
    opened_at: dt.datetime
    closed_at: dt.datetime | None = None
    close_kind: str | None = None  # person or auto
    closed_by: str | None = None
    close_note: str | None = None
    events: tuple = ()  # ItemEvent, oldest first (scorecard_signal only)


@dc.dataclass(frozen=True)
class RereviewStatus:
    source: str  # a collector part: repo_admins, evidence, intake or events
    problem: str | None
    counts: dict
    checked_at: dt.datetime
    last_ok_at: dt.datetime | None = None  # the last time the part finished with no problem


def event_row(doc, run_repo, run_id, now, source="central") -> RereviewEvent:
    """A valid re-review-event/v1 as the store keeps it. Raises ValueError for anything else: the schema promises code-written strings."""
    from ..contracts import validate
    problems = validate("re-review-event.v1", doc)
    if problems or not REPO_NAME.fullmatch(run_repo or "") or type(run_id) is not int or run_id < 1 or source not in SOURCES:
        raise ValueError(f"not a valid re-review event: {problems[0] if problems else 'run'}")
    created = dt.datetime.strptime(doc["created"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    return RereviewEvent(doc["repo"], doc["head_sha"], doc["pr"], doc["level"], doc["floor"], doc["complete"], created, doc["tree_sha"],
                         doc["scorecard_version"], tuple(dict(s) for s in doc["signals"]), run_repo, run_id, now, source)


def item_event(event):
    """An item's (head_sha, matched) or (head_sha, matched, source): (head_sha, matched, source), central by default."""
    return (event[0], tuple(event[1]), event[2] if len(event) > 2 else "central")


def check_item(repo, reason, key, event) -> None:
    """An item names a repo and a known reason; a date reason's key is its ISO date and it carries no event; a scorecard item opens
    with its first event, whose head SHA is its key."""
    if not REPO_NAME.fullmatch(repo or "") or reason not in REREVIEW_REASONS:
        raise ValueError("an item names a repo and a known reason")
    if reason in DATE_REASONS:
        try:
            ok = dt.date.fromisoformat(key).isoformat() == key and event is None
        except (TypeError, ValueError):
            ok = False
    else:
        ok = (isinstance(key, str) and SHA40.fullmatch(key) is not None and event is not None and len(event) in (2, 3) and event[0] == key
              and (len(event) == 2 or event[2] in SOURCES))
    if not ok:
        raise ValueError("an item's key is its review date, or its first event's head SHA")
    if event is not None:
        check_matched(event[1])


def check_matched(matched) -> None:
    if not matched or len(matched) > MAX_MATCHED or not all(isinstance(s, str) and SIGNAL_ID.fullmatch(s) for s in matched):
        raise ValueError("an item's event names the signal IDs that matched")


def check_note(note) -> None:
    if not isinstance(note, str) or not MIN_NOTE <= len(note) <= MAX_NOTE or "\n" in note or "\r" in note:
        raise ValueError(f"a note is one line of {MIN_NOTE} to {MAX_NOTE} characters")


@dc.dataclass(frozen=True)
class FailedRun:
    """A completed run that didn't succeed (or one still going after a day): re-checked until a later attempt succeeds, its artifacts are
    gone, or GitHub would no longer re-run it (a cache)."""
    repo: str
    run_id: int
    attempt: int
    last_attempt_at: dt.datetime
    retention_days: int
    seen_at: dt.datetime | None = None
    checked_at: dt.datetime | None = None


def check_failed_run(repo, run_id, attempt, retention_days) -> None:
    if (not REPO_NAME.fullmatch(repo or "") or type(run_id) is not int or run_id < 1 or type(attempt) is not int or attempt < 1
            or type(retention_days) is not int or not 1 <= retention_days <= 400):
        raise ValueError("a failed run names its repo, a positive run id and attempt, and its artifacts' retention (1 to 400 days)")


def check_watermark(repo, run_id) -> None:
    if not REPO_NAME.fullmatch(repo or "") or type(run_id) is not int or run_id < 1:
        raise ValueError("a watermark names its repo and a positive run id")


def check_status(source, problem, counts) -> None:
    if source not in STATUS_PROBLEMS or (problem is not None and problem not in STATUS_PROBLEMS[source]) or not isinstance(counts, dict) \
            or not all(isinstance(k, str) and type(v) is int and v >= 0 for k, v in counts.items()):
        raise ValueError("a source status names its source, one of its problems or none, and whole-number counts")


class Store(Protocol):
    def sign_in(self, *, issuer, subject, display_name, email, groups, roles, now, max_age) -> tuple[Person, str, str]: ...
    def get_session(self, token, *, now, idle) -> Session | None: ...
    def end_session(self, token) -> None: ...
    def get_person(self, person_id) -> Person | None: ...
    def list_people(self) -> list[Person]: ...
    def put_login_state(self, token, state, *, expires_at) -> None: ...
    def take_login_state(self, token, *, kind, now) -> LoginState | None: ...
    def add_link(self, *, person_id, github_login, github_user_id, source, actor_id, reason, now) -> Link: ...
    def remove_link(self, *, link_id, actor_id, reason, now) -> Link: ...
    def links_for(self, person_id) -> list[Link]: ...
    def active_links(self) -> list[Link]: ...
    def replace_repo_admins(self, rows, *, observed_at) -> None: ...
    def admin_repos(self, github_user_ids) -> list[RepoAdmin]: ...
    def admin_repos_by_login(self, logins) -> list[RepoAdmin]: ...
    def admin_repos_of_person(self, person_id) -> list[RepoAdmin]: ...  # through the person's active links: by user ID, else by login
    def observed_repo(self, repo) -> str | None: ...  # the collector's name of a repo, matched case-insensitively, or None
    def create_registration(self, *, person_id, draft, now) -> Request: ...
    def create_binding(self, *, request_id, person_id, final, now, current=None) -> Request: ...  # approved, no decision (B1); audits current as before
    def get_request(self, request_id) -> Request | None: ...
    def requests_by(self, person_id) -> list[Request]: ...
    def requests_for_repo(self, repo) -> list[Request]: ...  # every kind, newest first, the repo matched case-insensitively
    def requests_in(self, states) -> list[Request]: ...
    def resubmit(self, *, request_id, person_id, draft, now) -> Request: ...
    def decide(self, *, request_id, actor_id, actor_groups, approve, note, final, now, floor_override=None) -> Request: ...
    def claim_approved(self, *, now) -> Request | None: ...
    def mark_pr_open(self, *, request_id, branch, pr_number, pr_url, head_sha, base_sha, now) -> Request: ...
    def mark_merged(self, *, request_id, merge_sha, now) -> Request: ...
    def mark_failed(self, *, request_id, reason, now) -> Request: ...
    def awaiting_evidence(self, limit: int = 50) -> list[Awaiting]: ...
    def add_evidence(self, *, request_id, submission, commit, evidence, floor_tier, floor_classification, rules_commit, now,
                     record_sha=None, record=None) -> Evidence: ...
    def add_evidence_problem(self, *, request_id, submission, problem, now) -> None: ...
    def evidence_for(self, request_id, submission) -> Evidence | None: ...
    def evidence_problem_for(self, request_id, submission) -> str | None: ...
    def audit_entries(self, limit: int = 200) -> list[AuditEntry]: ...
    def audit_for(self, subject_kind, subject_id) -> list[AuditEntry]: ...  # one subject's entries, oldest first
    def replace_intake_observed(self, rows, *, observed_at) -> None: ...
    def intake_observed(self, repo=None) -> list[IntakeObserved]: ...  # by lower(repo); one repo's (any case) when given
    def rereview_runs_seen(self, repo, run_ids) -> set[int]: ...
    def record_rereview_run(self, *, repo, run_id, outcome, now) -> None: ...
    def rereview_run_counts(self) -> dict[str, int]: ...
    def add_rereview_event(self, *, doc, run_repo, run_id, now, source="central") -> RereviewEvent | None: ...  # None: stored already
    def rereview_items(self, *, open_only=True) -> list[RereviewItem]: ...  # oldest first, with their events
    def open_rereview_item(self, *, repo, reason, key, now, event=None) -> RereviewItem: ...  # event: (head_sha, matched[, source])
    def add_rereview_item_event(self, *, item_id, head_sha, matched, now, source="central") -> RereviewItem: ...
    def close_rereview_item(self, *, item_id, actor_id, note, now, events_seen) -> RereviewItem: ...  # item_changed unless it still has that many
    def auto_close_rereview_item(self, *, item_id, note, now) -> RereviewItem: ...
    def set_rereview_status(self, *, source, problem, counts, now) -> None: ...
    def rereview_event(self, repo, head_sha, source="central") -> RereviewEvent | None: ...  # that source's event of the repo (any case) and head
    def rereview_watermarks(self) -> list[Watermark]: ...
    def hold_pending_event(self, *, source, run_repo, run_id, doc, now) -> None: ...  # once per run
    def pending_events(self) -> list[PendingEvent]: ...
    def drop_pending_event(self, *, run_repo, run_id) -> None: ...
    def rereview_watermark(self, repo) -> int | None: ...  # every run of this source above it is done
    def set_rereview_watermark(self, *, repo, run_id, now) -> None: ...  # never lowers it; the same value notes when a walk finished
    def record_failed_run(self, *, repo, run_id, attempt, last_attempt_at, retention_days, now) -> None: ...  # or update its attempt
    def failed_runs(self, repo=None) -> list[FailedRun]: ...  # one source's, or every source's
    def note_stuck_run(self, *, repo, run_id, now) -> int: ...  # one more pass GitHub couldn't answer for this run: how many now
    def stuck_runs(self, repo) -> dict[int, int]: ...
    def clear_stuck_run(self, *, repo, run_id) -> None: ...
    def drop_failed_run(self, *, repo, run_id) -> None: ...
    def rereview_status(self) -> dict[str, RereviewStatus]: ...
