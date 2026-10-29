"""The app page (spec 2026-10-29 app bindings): an app's two connections, picked from the directory, recorded by the writer. The
server checks everything again: the repo comes from APP_ADMIN (the name the portal stores), the current binding from the store,
and the posted directory id goes through next_binding. Nothing in a form is trusted but the one choice."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..binding_record import BindingRefused, next_binding
from ..bindings import effects
from ..ids import new_id
from ..store.base import OPEN_STATES, Conflict
from .access import ADMIN, APP_ADMIN
from .render import page

router = APIRouter()
IN_PROGRESS = "A change to this app's connections is in progress. Wait for it to finish."
NOT_REPO_ADMIN = "Only an admin of the repository, as the portal last saw it, can change its connections."


def binding_state(store, repo):
    """(current binding or None, the open binding request or None, the latest change if it failed or None, the portal's merged
    registration or None). requests_for_repo is newest first."""
    found = store.requests_for_repo(repo)
    changes = [r for r in found if r.kind == "binding"]
    current = next((r.final for r in changes if r.state == "merged"), None)
    open_one = next((r for r in changes if r.state in OPEN_STATES), None)
    failed = changes[0] if changes and changes[0].state == "failed" else None
    registration = next((r for r in found if r.kind == "registration" and r.state == "merged"), None)
    return current, open_one, failed, registration


def app_page(request, status=200, errors=()):
    state, repo = request.app.state, request.state.app_repo
    directory, org = state.settings.directory, state.settings.org
    current, open_one, failed, registration = binding_state(state.store, repo)
    identity = (current or {}).get("azure_identity")
    owner_name, repo_name = repo.split("/")
    return page(request, "app.html", status, repo=repo, owner_name=owner_name, repo_name=repo_name, registration=registration, directory=directory,
                current=current, open_request=open_one, failed=failed, can_change=request.state.app_repo_admin,
                principals=directory.principals_for(org, repo) if directory else [],
                warehouses=directory.warehouses_for(identity["directory_id"]) if directory and identity else [],
                # what the release renders from the binding: the pending one once it is recorded, else the current one
                effects=effects(open_one.final if open_one else current), errors=list(errors))


@router.get("/apps/{owner}/{repo}")
def show(request: Request, owner: str, repo: str, principal=APP_ADMIN):
    return app_page(request)


def _change(request, principal, **choice):
    # the path parameters are not used: request.state.app_repo is the checked, stored owner/name
    state, repo = request.app.state, request.state.app_repo
    if state.settings.directory is None:
        return page(request, "error.html", 404, message="Not found.")
    if not request.state.app_repo_admin:  # the writer records a change only for the repo's admin (its recheck); say so now
        return app_page(request, 403, [NOT_REPO_ADMIN])
    current, open_one, _, _ = binding_state(state.store, repo)
    if open_one is not None:
        return app_page(request, 409, [IN_PROGRESS])
    request_id, now = new_id("r"), state.clock()
    try:
        final = next_binding(current=current, repo=repo, org=state.settings.org, directory=state.settings.directory,
                             person_id=principal.person.id, request_id=request_id, now=now, **choice)
    except BindingRefused as exc:
        return app_page(request, 400, [str(exc)])
    try:
        state.store.create_binding(request_id=request_id, person_id=principal.person.id, final=final, now=now, current=current)
    except Conflict as exc:
        if exc.code != "open_request_exists":  # request_exists, unknown_person: a bug, not a race the person can retry
            raise
        return app_page(request, 409, [IN_PROGRESS])
    owner_name, name = repo.split("/")
    return RedirectResponse(f"/apps/{owner_name}/{name}", status_code=303)


@router.post("/apps/{owner}/{repo}/azure-identity")
def link_identity(request: Request, owner: str, repo: str, principal_id: str = Form(""), principal=APP_ADMIN):
    return _change(request, principal, principal_id=principal_id)


@router.post("/apps/{owner}/{repo}/databricks")
def link_databricks(request: Request, owner: str, repo: str, warehouse_id: str = Form(""), principal=APP_ADMIN):
    return _change(request, principal, warehouse_id=warehouse_id)


@router.post("/apps/{owner}/{repo}/remove")
def remove(request: Request, owner: str, repo: str, what: str = Form(""), principal=APP_ADMIN):
    return _change(request, principal, remove=what)


@router.get("/admin/directory")
def directory_page(request: Request, principal=ADMIN):
    settings = request.app.state.settings
    return page(request, "admin_directory.html", directory=settings.directory, org=settings.org)
