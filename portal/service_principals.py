"""Creating a service principal (spec 2026-11-16): the form's rules (S1, S3, S11), pure and the same for the page and its POST; a
request's state in words (the card and the request page); what its ServiceNow task and creation note say, and the replay of the
store's tasks, mock decisions and sent notes into a fresh mock (Replayed: the collector's ServiceNow in the mock). The tier comes
from the catalog, never the browser: from the form only the name, the ticked scope ids and the justification are used. The web and the
collector both import this module (never each other)."""

import logging
import re
import unicodedata

from .outbox import neutral
from .servicenow import MAX_DESCRIPTION, MAX_SHORT_DESCRIPTION
from .store.base import MAX_JUSTIFICATION, SP_NAME, SP_SCHEMA

log = logging.getLogger(__name__)
MAX_SCOPES = 64  # values taken from one POST (decided while planning: item 21)
NO_SCOPE = "Choose at least one scope."
TOO_MANY = f"Choose at most {MAX_SCOPES} scopes."
UNKNOWN_SCOPE = "{scope} is not a scope the platform offers."
REPEATED_SCOPE = "{scope} is ticked twice."
NEEDS_WHY = "Say why this app needs {scopes}."
TOO_LONG = f"Keep the justification to {MAX_JUSTIFICATION} characters."
BAD_NAME = "The name is {prefix} and then lowercase letters, digits and -, at most 64 characters after sp-."
NAME_TAKEN = "{name} is already used: choose another name."
NO_SERVICENOW = "Requests for higher-risk scopes need ServiceNow, which isn't set up here."
BAD_CHARACTERS = "Write the justification without control or invisible formatting characters."


def unsafe_text(text: str) -> bool:
    """Whether a person's text holds a control character but a newline or a tab, a format character (bidi overrides, zero-width
    characters) or a surrogate: refused (final review M2), never stored (Postgres refuses a NUL) or sent to ServiceNow."""
    return any((unicodedata.category(c) == "Cc" and c not in "\n\t") or unicodedata.category(c) in ("Cf", "Cs") for c in text)


def name_prefix(repo: str) -> str:
    """sp-<repo name>-: the repo's name (without the org) in lowercase, each character outside a-z, 0-9 and - made a - (decided while
    planning: item 16)."""
    return "sp-" + re.sub(r"[^a-z0-9-]", "-", repo.split("/", 1)[1].lower()) + "-"


def name_problem(repo, name, taken) -> str | None:
    """Why the name can't be used (None: it can). taken: every name and id already used, in any case (the mock directory's, the store's
    principals and open requests)."""
    prefix = name_prefix(repo)
    if not isinstance(name, str) or not SP_NAME.fullmatch(name) or not name.startswith(prefix) or len(name) == len(prefix):
        return BAD_NAME.format(prefix=prefix)
    if name.lower() in {t.lower() for t in taken}:
        return NAME_TAKEN.format(name=name)
    return None


def form_problems(catalog, repo, name, scopes, justification, *, taken, servicenow) -> tuple:
    """(the draft, its first state, []) when the form is good, else (None, None, the problems in the form's order). servicenow: whether
    ServiceNow is set up here (a requestable scope needs it; S3 and Errors). With no requestable scope the justification is dropped
    (decided while planning: item 17)."""
    problems = []
    problem = name_problem(repo, name, taken)
    if problem:
        problems.append(problem)
    if not scopes:
        problems.append(NO_SCOPE)
    if len(scopes) > MAX_SCOPES:
        return None, None, problems + [TOO_MANY]
    seen, repeated = set(), []
    for s in scopes:
        if s in seen and s not in repeated:
            repeated.append(s)
        seen.add(s)
    problems += [REPEATED_SCOPE.format(scope=s[:80]) for s in repeated]
    problems += [UNKNOWN_SCOPE.format(scope=s[:80]) for s in dict.fromkeys(scopes) if catalog.scope(s) is None]
    requestable = [s for s in sorted(seen) if catalog.scope(s) is not None and catalog.scope(s).tier == "requestable"]
    why = (justification or "").replace("\r\n", "\n").strip()
    if requestable and not why:
        problems.append(NEEDS_WHY.format(scopes=" and ".join(requestable)))
    if requestable and len(why) > MAX_JUSTIFICATION:
        problems.append(TOO_LONG)
    if requestable and unsafe_text(why):
        problems.append(BAD_CHARACTERS)
    if requestable and not servicenow:
        problems.append(NO_SERVICENOW)
    if problems:
        return None, None, problems
    draft = {"schema": SP_SCHEMA, "repo": repo, "display_name": name, "scopes": sorted(seen)}
    if requestable:
        draft["justification"] = why
    return draft, ("pending" if requestable else "approved"), []


def state_words(req) -> str:
    """A service principal request's state as the card and the request page say it (spec "What the person sees")."""
    if req.state == "created":
        return "Created"
    if req.state == "approved":
        return "Creating…"
    if req.state == "rejected":
        return f"Rejected: {req.close_notes}" if req.close_notes else "Rejected"
    if req.task_number:
        return f"Waiting for approval in {req.task_number}"
    return "Waiting for approval: the ServiceNow task opens on the collector's next pass"


# ---- what ServiceNow is told (spec 2026-11-16 "Each collector pass"); every text passes outbox.neutral ----

def _link(base_url, request_id) -> str:
    return f"Golden road portal request: {base_url.rstrip('/')}/requests/{request_id}"


def task_short_description(req) -> str:
    return neutral(f"Service principal for {req.repo}: {req.display_name}")[:MAX_SHORT_DESCRIPTION]


def task_description(req, email, base_url, catalog) -> str:
    """The scopes (those that need approval marked, per the catalog now), the justification, the requester's email and the request's
    link. catalog None marks nothing."""
    lines = [f"Requested in the golden road portal by {email or 'a person with no email on record'} for {req.repo}.", "",
             f"Name: {req.display_name}", "Scopes:"]
    for scope_id in req.scopes:
        found = catalog.scope(scope_id) if catalog is not None else None
        mark = ("" if catalog is None else " (no longer in the catalog)" if found is None
                else " (needs approval)" if found.tier == "requestable" else "")
        lines.append(f"- {scope_id}{mark}")
    if req.justification is not None:
        lines += ["", "Justification:", req.justification]
    lines += ["", _link(base_url, req.id)]
    return neutral("\n".join(lines))[:MAX_DESCRIPTION]


def creation_note(req, principal, base_url) -> str:
    return neutral(f"Service principal {principal.display_name} created: client ID {principal.client_id}. Link it to test or prod on the "
                   f"app's page.\n\n{_link(base_url, req.id)}")


# ---- the mock's memory is per process (spec "The mock"): the store is the only thing the web and the collector both see ----

def replay_into(mock, store, base_url, catalog) -> None:
    """Into a fresh MockServiceNow: every task the portal opened for a service principal request (its number and sys_id from the
    store, its text rebuilt as it was sent), each mock decision the web stored, and each creation note the collector marked sent. A
    request whose task number or sys_id the mock holds already (a fixture task's) is skipped, with its decision and its note, and logged
    by its id only (Round 5 review): one such row never stops every task page and every collector pass."""
    requests = []
    for req in sorted((r for r in store.service_principal_requests() if r.task_number), key=lambda r: r.task_number):
        if mock.holds_task(req.task_number, req.task_sys_id):
            log.warning("service principals: request %s skipped in the mock's replay: the mock holds its task already", req.id)
        else:
            requests.append(req)
    replayed = {req.task_number for req in requests}
    principals = {p.request_id: p for p in store.service_principals()}
    for req in requests:
        cmdb = store.cmdb_record(req.repo)
        mock.add_task(number=req.task_number, sys_id=req.task_sys_id, correlation_id=req.id, short_description=task_short_description(req),
                      description=task_description(req, store.person_email(req.requested_by), base_url, catalog),
                      ci=cmdb.sys_id if cmdb is not None and cmdb.sys_id else None)
    for decision in (d for d in store.mock_task_decisions() if d.task_number in replayed):
        mock.decide_task(decision.task_number, decision.state, decision.close_notes,
                         decided_by_email=store.person_email(decision.decided_by))  # closed by the decider's mock user (final review I2)
    mock.replay([(req.task_sys_id, "note", creation_note(req, principals[req.id], base_url)) for req in requests
                 if req.note_sent_at is not None and req.id in principals])


class Replayed:
    """The collector's ServiceNow in the mock: each client() is a fresh mock with the store's tasks, decisions and notes replayed, so the
    web's Approve and Reject (another process) reach it. The settings' mock is never written to."""

    def __init__(self, mock, store, base_url, catalog):
        self.mock, self.store, self.base_url, self.catalog = mock, store, base_url, catalog

    def client(self):
        fresh = self.mock.fresh()
        replay_into(fresh, self.store, self.base_url, self.catalog)
        return fresh.client()
