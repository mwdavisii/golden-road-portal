"""Create a service principal (spec 2026-11-16). The app's admin (APP_ADMIN: the repo's admin as the collector last saw them, or a
portal admin; S2) ticks scopes from the platform's catalog; the server decides from the catalog whether the request is approved
(pre-approved scopes only) or pending (a ServiceNow task decides it), and records it. The collector does the rest (S4). From the form
only the name, the ticked scope ids and the justification are read."""

from fastapi import APIRouter, Depends, Form, Request

from .. import service_principals as sp
from ..ids import new_id
from ..lifecycle import decommissioned_words
from ..servicenow_mock import MockServiceNow
from ..store.base import MAX_CLOSE_NOTES, TASK_NUMBER, Conflict
from .access import ADMIN, APP_ADMIN
from .flash import redirect
from .render import page

router = APIRouter()
NOT_HERE = "Not found."


async def _posted_scopes(request: Request) -> list:
    """Every ticked scope as posted, in order (a checkbox list: Form() would keep one). Starlette caches the parsed form."""
    return [v for v in (await request.form()).getlist("scope") if isinstance(v, str)]


FORM_SCOPES = Depends(_posted_scopes)


def available(settings) -> bool:
    """The directory and its catalog are set up here (PORTAL_DIRECTORY=mock); without them nothing here shows (Errors)."""
    return settings.directory is not None and settings.graph_scopes is not None


def taken_names(settings, store) -> set:
    """Every name and id already used (S11): the mock directory's, the store's principals and its open requests."""
    names = {p.display_name for p in settings.directory.principals} | {p.id for p in settings.directory.principals}
    names |= {p.display_name for p in store.service_principals()}
    return names | {r.display_name for r in store.service_principal_requests(states=("pending", "approved"))}


def form_page(request, status=200, errors=(), form=None):
    state, repo = request.app.state, request.state.app_repo
    owner_name, repo_name = repo.split("/")
    catalog = state.settings.graph_scopes
    return page(request, "service_principal_new.html", status, repo=repo, owner_name=owner_name, repo_name=repo_name,
                prefix=sp.name_prefix(repo), approved=catalog.approved, requestable=catalog.requestable, errors=list(errors),
                form=form or {"name": sp.name_prefix(repo), "scopes": [], "justification": ""},
                servicenow=state.settings.servicenow_api is not None, max_justification=sp.MAX_JUSTIFICATION)


@router.get("/apps/{owner}/{repo}/service-principals/new")
def new(request: Request, owner: str, repo: str, principal=APP_ADMIN):
    if not available(request.app.state.settings):
        return page(request, "error.html", 404, message=NOT_HERE)
    return form_page(request)


@router.post("/apps/{owner}/{repo}/service-principals/new")
def create(request: Request, owner: str, repo: str, name: str = Form(""), justification: str = Form(""), principal=APP_ADMIN,
           scopes: list = FORM_SCOPES):
    state, repo = request.app.state, request.state.app_repo
    settings = state.settings
    if not available(settings):
        return page(request, "error.html", 404, message=NOT_HERE)
    name = name.strip()
    form = {"name": name, "scopes": scopes, "justification": justification}
    draft, first, problems = sp.form_problems(settings.graph_scopes, repo, name, scopes, justification,
                                              taken=taken_names(settings, state.store), servicenow=settings.servicenow_api is not None)
    if problems:
        return form_page(request, 400, problems, form)
    try:
        req = state.store.create_service_principal_request(request_id=new_id("r"), person_id=principal.person.id, repo=repo, draft=draft,
                                                           state=first, now=state.clock())
    except Conflict as exc:
        if exc.code == "name_taken":  # taken since the check above
            return form_page(request, 400, [sp.NAME_TAKEN.format(name=name)], form)
        if exc.code == "decommissioned":  # rule 4 (spec 2026-11-02): nothing more for this repo
            return form_page(request, 409, [decommissioned_words(state.store.lifecycle_state(repo))], form)
        raise
    return redirect(f"/requests/{req.id}", "service_principal")


def request_page(request, req, principal):
    """A service principal request's page (spec "What the person sees"): its scopes, its state and, once it exists, its ServiceNow task
    (a link to the mock's task page for an admin), and the principal's client ID once it is created."""
    store, settings = request.app.state.store, request.app.state.settings
    found = store.service_principal_request(req.id)
    made = next((p for p in store.service_principals(found.repo) if p.request_id == found.id), None)
    catalog = settings.graph_scopes
    scopes = []
    for scope_id in found.scopes:
        entry = catalog.scope(scope_id) if catalog is not None else None
        scopes.append({"id": scope_id, "label": entry.label if entry else scope_id, "needs_approval": bool(entry and entry.tier == "requestable")})
    retrying = found.attempts > 0 and (found.state in ("pending", "approved")
                                       or (found.state == "created" and found.task_number is not None and found.note_sent_at is None))
    return page(request, "service_principal_request.html", req=found, words=sp.state_words(found), scopes=scopes, made=made,
                retrying=retrying, mock_task=principal.is_admin and isinstance(settings.servicenow_api, MockServiceNow))


def card(app_state, repo, principal, repo_admin) -> dict | None:
    """The Azure identity card's part (spec "What the person sees"; decided while planning: items 12, 20), or None without the
    directory: the repo's created principals, its other requests with their state in words (newest first; a link for the requester or
    a portal admin), and whether this person may create one."""
    settings, store = app_state.settings, app_state.store
    if settings.directory is None:
        return None
    others = [r for r in store.service_principal_requests(repo=repo) if r.state != "created"]
    return {"created": store.service_principals(repo),
            "requests": [{"id": r.id, "name": r.display_name, "state": r.state, "words": sp.state_words(r),
                          "linked": r.requested_by == principal.person.id or principal.is_admin} for r in reversed(others)],
            "can_create": settings.graph_scopes is not None and (repo_admin or principal.is_admin)}


# ---- the mock ServiceNow's approver (S6): an admin who isn't the requester; the decision is stored, both mocks replay it ----

OWN_TASK = "You asked for this service principal, so another admin decides its task."
DECIDED = "This task is decided already."
NEED_NOTES = "Say why it is rejected: the close notes are what the requester reads."
NOTES_BAD = "Write the close notes without control or invisible formatting characters."
NOTES_TOO_LONG = f"Keep the close notes to {MAX_CLOSE_NOTES} characters."


def _open_task(store, number):
    """The pending service principal request whose task this is, or None."""
    return next((r for r in store.service_principal_requests(states=("pending",)) if r.task_number == number), None)


def task_decision(store, number, principal) -> dict | None:
    """What the mock task page offers (decided while planning: item 13): None for a task that isn't an open service principal task or is
    decided already; {"own": True} for its requester, who may not decide it."""
    req = _open_task(store, number)
    if req is None or any(d.task_number == number for d in store.mock_task_decisions()):
        return None
    return {"own": req.requested_by == principal.person.id}


def _decide(request, principal, number, state, close_notes):
    settings, store = request.app.state.settings, request.app.state.store
    if not isinstance(settings.servicenow_api, MockServiceNow) or not TASK_NUMBER.fullmatch(number):
        return page(request, "error.html", 404, message=NOT_HERE)
    req = next((r for r in store.service_principal_requests() if r.task_number == number), None)
    if req is None:
        return page(request, "error.html", 404, message=NOT_HERE)
    if req.requested_by == principal.person.id:
        return page(request, "error.html", 403, message=OWN_TASK)
    if req.state != "pending" or any(d.task_number == number for d in store.mock_task_decisions()):
        return page(request, "error.html", 409, message=DECIDED)
    close_notes = close_notes.replace("\r\n", "\n").strip()  # a browser's line breaks are CRLF: one character each, as the justification's
    if state != "3" and not close_notes:
        return page(request, "error.html", 400, message=NEED_NOTES)
    if len(close_notes) > MAX_CLOSE_NOTES:
        return page(request, "error.html", 400, message=NOTES_TOO_LONG)
    if sp.unsafe_text(close_notes):  # final review M2: never stored (Postgres refuses a NUL)
        return page(request, "error.html", 400, message=NOTES_BAD)
    try:
        store.add_mock_task_decision(task_number=number, state=state, close_notes=close_notes, actor_id=principal.person.id,
                                     now=request.app.state.clock())
    except Conflict as exc:
        if exc.code == "self_decision":
            return page(request, "error.html", 403, message=OWN_TASK)
        if exc.code in ("decided", "wrong_state"):
            return page(request, "error.html", 409, message=DECIDED)
        raise
    return redirect(f"/mock/servicenow/tasks/{number}", "task_decided")


@router.post("/mock/servicenow/tasks/{number}/approve")
def approve(request: Request, number: str, close_notes: str = Form(""), principal=ADMIN):
    return _decide(request, principal, number, "3", close_notes)


@router.post("/mock/servicenow/tasks/{number}/reject")
def reject(request: Request, number: str, close_notes: str = Form(""), principal=ADMIN):
    return _decide(request, principal, number, "4", close_notes)
