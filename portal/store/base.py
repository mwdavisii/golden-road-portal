"""The portal's records and the Store interface. MemoryStore and PostgresStore implement it, and
tests/test_portal_store_contract.py runs the same cases against both. Every change to people, links or requests writes its
audit entry in the same transaction (spec "Data": every decision needs a reason)."""

from __future__ import annotations

import dataclasses as dc
import datetime as dt
import hashlib
from typing import Protocol

OPEN_STATES = ("pending", "sent_back", "approved", "writing", "pr_open")
STATES = OPEN_STATES + ("merged", "failed")
LINK_SOURCES = ("github-oauth", "admin")


class Conflict(Exception):
    """The store refused a change. code: not_found, wrong_state, open_request_exists, link_taken, self_link, self_decision, repo_changed."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


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
    def create_registration(self, *, person_id, draft, now) -> Request: ...
    def get_request(self, request_id) -> Request | None: ...
    def requests_by(self, person_id) -> list[Request]: ...
    def requests_in(self, states) -> list[Request]: ...
    def resubmit(self, *, request_id, person_id, draft, now) -> Request: ...
    def decide(self, *, request_id, actor_id, actor_groups, approve, note, final, now) -> Request: ...
    def claim_approved(self, *, now) -> Request | None: ...
    def mark_pr_open(self, *, request_id, branch, pr_number, pr_url, head_sha, base_sha, now) -> Request: ...
    def mark_merged(self, *, request_id, merge_sha, now) -> Request: ...
    def mark_failed(self, *, request_id, reason, now) -> Request: ...
    def audit_entries(self, limit: int = 200) -> list[AuditEntry]: ...
