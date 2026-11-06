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
    """The store refused a change. code: not_found, wrong_state, open_request_exists, link_taken, self_link, self_decision, repo_changed, request_exists, unknown_person, evidence_exists, not_awaiting, no_evidence, stale_evidence, evidence_mismatch, below_floor, run_seen, item_exists, item_open, no_event, event_exists, closed, own_app, wrong_reason, wrong_role, item_changed, record_unreadable, decommissioned, command_open, not_lifecycle."""

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
    ownership_acknowledged_at: dt.datetime | None = None  # a registration's "I understand what owning this app means" (0011); None before


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
                              "failed"),
                   "cmdb": ("cmdb_unreachable", "cmdb_owner_unknown", "cmdb_refused", "failed")}
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
    source: str  # a collector part: repo_admins, evidence, intake, events or cmdb
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


CMDB_ERRORS = ("cmdb_unreachable", "cmdb_owner_unknown", "cmdb_refused")
CMDB_OPERATIONS = ("INSERT", "UPDATE", "NO_CHANGE")
SYS_ID = re.compile(r"[0-9a-f]{32}")
CI_NUMBER = re.compile(r"[A-Z]{2,8}[0-9]{4,12}")
HASH = re.compile(r"[0-9a-f]{64}")


@dc.dataclass(frozen=True)
class CmdbRecord:
    """An intake record as the collector last synced it to ServiceNow (a cache: the intake file in git is the truth, spec N1)."""
    repo: str
    sys_id: str | None
    number: str | None
    fields_hash: str | None  # of the last good sync
    last_operation: str | None
    last_synced_at: dt.datetime | None
    last_error: str | None  # one of CMDB_ERRORS, or None after a good sync
    last_error_at: dt.datetime | None


def _matches(pattern, value) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def check_cmdb_sync(repo, sys_id, number, fields_hash, operation) -> None:
    """A good sync names its repo, the CI's sys_id and number, the fields hash and the operation (migration 0011's checks)."""
    if not (_matches(REPO_NAME, repo) and _matches(SYS_ID, sys_id) and _matches(CI_NUMBER, number) and _matches(HASH, fields_hash)
            and operation in CMDB_OPERATIONS):
        raise ValueError("a CMDB sync names its repo, the CI's sys_id and number, the fields hash and the operation")


def check_cmdb_error(repo, error) -> None:
    if not _matches(REPO_NAME, repo) or error not in CMDB_ERRORS:
        raise ValueError("a CMDB error names its repo and one of the known errors")


# ---- lifecycle (spec 2026-11-02, migration 0012): a request's steps, the commands the web asks the writer for, the outbox ----

LIFECYCLE_ACTIONS = ("suspend", "resume", "decommission")
LIFECYCLE_STATES = ("active", "suspended", "decommissioned")
# Rule 7's order. A decommission archives the repository before the factory PR (decided by the lead while Mike was away): archived, the
# app's release workflow can't add an environment back to the deploy repo while the factory record still says it is live. A step leaves
# planned only once every earlier one is done (both stores and 0012's step guard).
ACTION_STEPS = {"suspend": ("deploy",), "resume": ("deploy",), "decommission": ("deploy", "archive", "factory")}
STATE_AFTER = {"suspend": "suspended", "resume": "active", "decommission": "decommissioned"}  # the state each action applies
STEP_STATES = ("planned", "pr_open", "done", "failed", "closed")
# Every move a step may make (0012's lifecycle_step guard holds the same): done and closed are for good; back to planned is a retry
# (from failed) or a rebuild on a new main (from pr_open, or from planned: a branch left on an old main by a pass that stopped before it
# recorded its PR), and starts a new attempt with no pull request.
STEP_TRANSITIONS = frozenset({("planned", "pr_open"), ("planned", "done"), ("pr_open", "done"), ("planned", "failed"), ("pr_open", "failed"),
                              ("pr_open", "planned"), ("failed", "planned"), ("planned", "planned"), ("planned", "closed"), ("pr_open", "closed"),
                              ("failed", "closed")})
# "command": a command the writer finished without carrying it out (refused: not an admin; too late; nothing to retry), said in a note;
# "rebuilt": main moved, so the writer closed its pull request and starts the step again on a new attempt; "stopped": a decommission an
# admin stopped after its deploy merge (the task stays open)
EVENT_STEPS = ("requested", "pr_opened", "merged", "factory_merged", "archived", "failed", "retried", "abandoned", "completed",
               "ci_corrected", "command", "rebuilt", "stopped")
# stop: the way out of a decommission whose deploy step is done (too late to abandon) but whose archive or factory step can't finish
COMMANDS = ("retry", "abandon", "stop")
TASK_NUMBER = re.compile(r"SCTASK[0-9]{7}")
PERSON_ID = re.compile(r"p_[0-9a-f]{32}")
MIN_REASON, MAX_REASON = 10, 1000
MAX_EVENT_NOTE = 3000  # a work note: the reason (1000) and what the platform did, with room
MAX_STEP_REASON = 300
LIFECYCLE_KEYS = frozenset({"action", "task", "task_sys_id", "reason"})


@dc.dataclass(frozen=True)
class LifecycleState:
    """What the platform applied to an app, in git (L12). No row is an active app that never had a lifecycle action."""
    repo: str
    state: str  # active, suspended or decommissioned (for good)
    task: str | None  # the ServiceNow task of the action that set it; None when active
    since: dt.datetime
    request: str  # the lifecycle request that set it: rule 9's "latest lifecycle task"


@dc.dataclass(frozen=True)
class LifecycleStep:
    request_id: str
    step: str  # deploy, archive or factory
    position: int  # 1, 2, 3: ACTION_STEPS's order
    state: str  # one of STEP_STATES
    attempt: int = 1
    branch: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    head_sha: str | None = None  # the commit the step's PR was opened with: the only one merged
    base_sha: str | None = None
    merge_sha: str | None = None
    failure_reason: str | None = None
    updated_at: dt.datetime | None = None


@dc.dataclass(frozen=True)
class LifecycleCommand:
    """What an admin asked the writer to do with a lifecycle request (the web holds no GitHub credential): retry its failed step,
    abandon it (rule 8), or stop a decommission after its deploy merge. At most one open per request."""
    id: int
    request_id: str
    command: str  # retry, abandon or stop
    requested_by: str
    requested_at: dt.datetime
    done_at: dt.datetime | None = None


@dc.dataclass(frozen=True)
class LifecycleEvent:
    """One step of a lifecycle request, as a work note for its ServiceNow task (the outbox, L11). The collector sends a request's events
    in id order and marks each sent; a failure keeps it unsent with its attempts and last error, and the next pass tries again."""
    id: int
    request_id: str
    step: str  # one of EVENT_STEPS; completed closes the task
    note: str
    task_sys_id: str  # the request's task, set by the store from the request
    created_at: dt.datetime
    sent_at: dt.datetime | None = None
    attempts: int = 0
    last_error: str | None = None


def check_lifecycle(request_id, person_id, repo, final) -> None:
    """A lifecycle request's id has the portal's format, it names a person and a repo, and its record is exactly an action, a task (SCTASK
    and 7 digits), the task's 32-hex sys_id and a one-line reason of 10 to 1000 characters (0012's insert guard holds the same)."""
    if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
        raise ValueError("a lifecycle request's id is not a portal request id")
    if not isinstance(person_id, str) or not PERSON_ID.fullmatch(person_id) or not isinstance(repo, str) or not REPO_NAME.fullmatch(repo):
        raise ValueError("a lifecycle request names its person and its repo")
    reason = final.get("reason") if isinstance(final, dict) else None
    if (not isinstance(final, dict) or set(final) != LIFECYCLE_KEYS or final["action"] not in LIFECYCLE_ACTIONS
            or not isinstance(final["task"], str) or not TASK_NUMBER.fullmatch(final["task"])
            or not isinstance(final["task_sys_id"], str) or not SYS_ID.fullmatch(final["task_sys_id"])
            or not isinstance(reason, str) or not MIN_REASON <= len(reason) <= MAX_REASON or "\n" in reason or "\r" in reason):
        raise ValueError("a lifecycle request's record is an action, a task, the task's sys_id and a one-line reason of 10 to 1000 characters")


def check_event(step, note) -> None:
    if step not in EVENT_STEPS or not isinstance(note, str) or not 1 <= len(note) <= MAX_EVENT_NOTE:
        raise ValueError(f"a lifecycle event names a known step and a note of 1 to {MAX_EVENT_NOTE} characters")


PR_FIELDS = ("branch", "pr_number", "pr_url", "head_sha", "base_sha")


def moved_step(kept, to_state, now, fields):
    """The step after one of STEP_TRANSITIONS, with only the fields that move may set: pr_open needs all of PR_FIELDS, done may name
    its merge commit (an archive or an empty deploy step has none), failed needs its reason, and planned starts a new attempt with
    no pull request (a retry or a rebuild); closed drops a failed step's reason. Raises ValueError for anything else."""
    allowed = {"pr_open": set(PR_FIELDS), "done": {"merge_sha"}, "failed": {"failure_reason"}, "planned": set(), "closed": set()}[to_state]
    if set(fields) - allowed:
        raise ValueError(f"a step moving to {to_state} sets only {sorted(allowed)}")
    if to_state == "pr_open" and (set(fields) != set(PR_FIELDS) or not SHA40.fullmatch(fields["head_sha"] or "")
                                  or not SHA40.fullmatch(fields["base_sha"] or "") or type(fields["pr_number"]) is not int):
        raise ValueError("an open step's pull request names its branch, number, URL and its head and base commits")
    if to_state == "done" and fields.get("merge_sha") is not None and not SHA40.fullmatch(fields["merge_sha"]):
        raise ValueError("a merge commit is 40 hex characters")
    if to_state == "failed" and not (isinstance(fields.get("failure_reason"), str) and 1 <= len(fields["failure_reason"]) <= MAX_STEP_REASON):
        raise ValueError("a failed step records a reason of 1 to 300 characters")
    if to_state == "planned":
        return dc.replace(kept, state="planned", attempt=kept.attempt + 1, failure_reason=None, merge_sha=None, updated_at=now,
                          **{f: None for f in PR_FIELDS})
    if to_state == "closed":  # a failed step's reason goes with its state (0012: only a failed step has one)
        return dc.replace(kept, state="closed", failure_reason=None, updated_at=now)
    return dc.replace(kept, state=to_state, updated_at=now, **fields)


class Store(Protocol):
    def sign_in(self, *, issuer, subject, display_name, email, groups, roles, now, max_age) -> tuple[Person, str, str]: ...
    def get_session(self, token, *, now, idle) -> Session | None: ...
    def end_session(self, token) -> None: ...
    def get_person(self, person_id) -> Person | None: ...
    def person_email(self, person_id) -> str | None: ...  # the collector's one read of person (0011 grants it id and email)
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
    def create_registration(self, *, person_id, draft, now, ownership_acknowledged_at=None) -> Request: ...
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
    def record_cmdb_sync(self, *, repo, sys_id, number, fields_hash, operation, now) -> None: ...  # clears last_error
    def record_cmdb_error(self, *, repo, error, now) -> None: ...  # keeps the CI's sys_id, number and hash
    def cmdb_records(self) -> list[CmdbRecord]: ...  # by lower(repo)
    def cmdb_record(self, repo) -> CmdbRecord | None: ...  # any case
    def create_lifecycle(self, *, request_id, person_id, repo, final, note, now) -> Request: ...  # approved; its "requested" event too
    def lifecycle_state(self, repo) -> LifecycleState | None: ...  # any case
    def lifecycle_states(self) -> list[LifecycleState]: ...  # by lower(repo)
    # the writer's, while the request is writing: the state its action applies (STATE_AFTER), to the request's repo with its task (none for
    # active); the same again from the same request is kept as it is; decommissioned is for good
    def set_lifecycle_state(self, *, request_id, state, now) -> LifecycleState: ...
    def add_lifecycle_steps(self, *, request_id, steps, now) -> list[LifecycleStep]: ...  # all planned, in order; the request writing
    def lifecycle_steps(self, request_id) -> list[LifecycleStep]: ...  # by position
    # one of STEP_TRANSITIONS, while the request is writing; leaving planned (but for closed) once every earlier step is done
    def move_lifecycle_step(self, *, request_id, step, to_state, now, **fields) -> LifecycleStep: ...
    # move_lifecycle_step and add_lifecycle_event(step=event, note=note) in one transaction: both or neither (L11 never loses a note)
    def move_lifecycle_step_with_event(self, *, request_id, step, to_state, now, event, note, **fields) -> tuple[LifecycleStep, LifecycleEvent]: ...
    def add_lifecycle_command(self, *, request_id, command, actor_id, now) -> LifecycleCommand: ...
    def open_lifecycle_command(self, request_id) -> LifecycleCommand | None: ...
    def finish_lifecycle_command(self, *, command_id, now, note=None) -> None: ...  # with note: its "command" note, in one transaction
    # a retry carried out, in one transaction: the failed step back to planned (a new attempt), its "retried" note, and the retry command
    # finished; the step must have failed and the command be this request's open retry, or nothing changes
    def retry_lifecycle(self, *, request_id, command_id, step, note, now) -> LifecycleStep: ...
    # an abandon carried out, in one transaction: every step not done closed, the command finished, its "abandoned" note, and writing ->
    # failed ("abandoned")
    def abandon_lifecycle(self, *, request_id, command_id, note, now) -> Request: ...
    # a stop carried out, the same way (its "stopped" note, writing -> failed "stopped"); only for a decommission whose deploy step is
    # done: the state it set stays
    def stop_lifecycle(self, *, request_id, command_id, note, now) -> Request: ...
    def add_lifecycle_event(self, *, request_id, step, note, now) -> LifecycleEvent: ...
    def lifecycle_events(self, request_id=None, *, unsent=False) -> list[LifecycleEvent]: ...  # by id
    def mark_lifecycle_event_sent(self, *, event_id, now) -> None: ...  # in order: never while an earlier one of its request is unsent
    def mark_lifecycle_event_failed(self, *, event_id, error, now) -> None: ...  # attempts + 1, the error kept
    def writeback_lock(self): ...  # a context manager: True while this pass alone sends the outbox, False when another pass does
    # once every step is done, in one transaction: the state its action applies (set_lifecycle_state's, kept if this request set it
    # already), its completed note (closes the task), and writing -> merged with the last merge commit (or None: no step had a PR)
    def apply_lifecycle(self, *, request_id, merge_sha, note, now) -> Request: ...
