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
    """The store refused a change. code: not_found, wrong_state, open_request_exists, link_taken, self_link, self_decision, repo_changed, request_exists, unknown_person, evidence_exists, not_awaiting, no_evidence, evidence_mismatch, below_floor."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


MIN_OVERRIDE, MAX_OVERRIDE = 30, 1000


def check_approval(evidence, final, floor_override, actor_id):
    """The rules an approval must meet, the same in every Store (the database enforces them again): the evidence of the request's
    current submission exists, the record carries its commit and floor and the very override the request does, and a tier or
    classification below the floor needs the approver's own override of at least MIN_OVERRIDE characters (and none otherwise)."""
    from ..floor import below_floor
    if evidence is None:
        raise Conflict("no_evidence")
    if (final.get("evidence_commit"), final.get("floor_tier"), final.get("floor_classification")) != (
            evidence.commit, evidence.floor_tier, evidence.floor_classification) or final.get("floor_override") != floor_override:
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


@dc.dataclass(frozen=True)
class Awaiting:
    """A pending registration that has no evidence yet. Never carries the draft: the collector may not read it."""
    request_id: str
    repo: str
    submission: int


PROBLEMS = ("rules_missing", "rules_invalid", "rules_unreadable", "repo_unreadable")


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
    def add_evidence(self, *, request_id, submission, commit, evidence, floor_tier, floor_classification, rules_commit, now) -> Evidence: ...
    def add_evidence_problem(self, *, request_id, submission, problem, now) -> None: ...
    def evidence_for(self, request_id, submission) -> Evidence | None: ...
    def evidence_problem_for(self, request_id, submission) -> str | None: ...
    def audit_entries(self, limit: int = 200) -> list[AuditEntry]: ...
