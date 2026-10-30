"""The app page (spec 2026-10-29 app bindings): an app's two connections, picked from the directory, recorded by the writer. The
server checks everything again: the repo comes from APP_ADMIN (the name the portal stores), the current binding from the store,
and the posted directory id goes through next_binding. Nothing in a form is trusted but the one choice and the environment.

Each environment (test, prod) has its own connections (spec 2026-10-29 GitOps deploy repo, G3 and G9): every route takes
`environment` as a query (GET) or form (POST) value, test by default, and any other value is a 400.

Delinea Secret Server secrets (spec 2026-10-29 Delinea secrets, "The portal"): the page searches as the person (their Secret Server
user, through a GitHub link they verified with OAuth) and asks the platform's account whether each secret is shared with it. A
submit is checked again by next_binding, whatever the form posts. No page, form or request ever carries a value."""

import dataclasses as dc
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse

from ..binding_record import ENVIRONMENTS, BindingRefused, environment_state, next_binding, secret_tokens
from ..bindings import effects
from ..ids import new_id
from ..secretserver import SecretServerError
from ..store.base import Conflict
from .access import ADMIN, APP_ADMIN
from .render import page

router = APIRouter()
IN_PROGRESS = "A change to this app's connections is in progress. Wait for it to finish."
NOT_REPO_ADMIN = "Only an admin of the repository, as the portal last saw it, can change its connections."
SEARCH_MAX = 100  # characters of search text
SHOWN_MAX = 20  # search results shown: one page of the search, never every page
DIGITS = re.compile(r"[1-9][0-9]{0,9}")  # ASCII only: int() takes other scripts' digits
NO_LINK = ("Link your GitHub account (verified) first: the portal finds your Secret Server user through it. An admin-made link "
           "is not enough; link it yourself from your page.")
NO_SECRET_SERVER = "Secret Server is not set up here."
SECRET_SERVER_DOWN = "Secret Server could not answer: try again."


@dc.dataclass(frozen=True)
class SecretResult:
    id: int
    name: str
    folder: str
    template_name: str
    shared: bool
    fields: tuple  # the template's non-file fields, only for a shared secret


def binding_state(store, repo, environment):
    """(current binding or None, the open binding request or None, the latest change if it failed or None, the portal's merged
    registration or None) for one environment. requests_for_repo is newest first."""
    found = store.requests_for_repo(repo)
    current, open_one, failed = environment_state(found, environment)
    registration = next((r for r in found if r.kind == "registration" and r.state == "merged"), None)
    return current, open_one, failed, registration


def app_url(repo, environment):
    owner_name, name = repo.split("/")
    return f"/apps/{quote(owner_name)}/{quote(name)}?environment={environment}"


async def _posted_environment(request: Request) -> str:
    """The form's environment exactly as posted, test when the form has none. FastAPI's Form() treats an empty field as missing,
    and an empty environment is not test: it is refused like any other value. Starlette caches the parsed form."""
    values = (await request.form()).getlist("environment")
    return "test" if not values else values[0] if len(values) == 1 and isinstance(values[0], str) else ""


FORM_ENVIRONMENT = Depends(_posted_environment)


def _bad_environment(request):
    return page(request, "error.html", 400, message="The environment must be test or prod.")


def secret_user(request):
    """The signed-in person's Secret Server user, or None: in the mock, through a GitHub link they verified with OAuth."""
    state = request.app.state
    return state.settings.secret_server.user_for(state.store.links_for(request.state.principal.person.id))


def _parse_search(q, folder):
    """(text, folder id or None, error or None) from the page's search query."""
    if not isinstance(q, str) or len(q) > SEARCH_MAX:
        return "", None, f"Search text is at most {SEARCH_MAX} characters."
    if folder in ("", None):
        return q, None, None
    if not isinstance(folder, str) or not DIGITS.fullmatch(folder):
        return q, None, "Choose a folder from the list."
    return q, int(folder), None


def secrets_view(request, search):
    """What the secrets section shows: {"secret_results", "folders", "secret_note", "searched", "secrets_truncated"}. Only the
    repo's admin (the one who can add) gets folders and results. Secret Server is searched only when the person submitted a
    search: `search` is (text, folder id), or None for none (a plain page load, an error re-render)."""
    state = request.app.state
    view = {"secret_results": [], "folders": {}, "secret_note": None, "searched": search is not None, "secrets_truncated": False}
    if state.settings.secret_server is None or not request.state.app_repo_admin:
        return view
    user = secret_user(request)
    if user is None:
        return {**view, "secret_note": NO_LINK}
    failure, results, more = None, [], False
    try:
        with secret_tokens(state.settings.secret_server, user) as (client, person_token, platform_token):
            folders = client.folders(person_token)
            if search is not None:
                found, more = client.search_first(person_token, text=search[0], folder_id=search[1], take=SHOWN_MAX)
                fields = {}
                for s in found:
                    shared = client.shared_with_platform(platform_token, s.id)
                    if shared and s.template_id not in fields:
                        fields[s.template_id] = tuple(client.template_fields(platform_token, s.template_id))
                    results.append(SecretResult(s.id, s.name, folders.get(s.folder_id, str(s.folder_id)), s.template_name, shared,
                                                fields[s.template_id] if shared else ()))
    except SecretServerError:
        failure = SECRET_SERVER_DOWN  # never the error's text
    if failure:
        return {**view, "secret_note": failure}
    return {**view, "secret_results": results, "folders": folders, "secrets_truncated": more}


def app_page(request, environment, status=200, errors=(), search=None):
    state, repo = request.app.state, request.state.app_repo
    directory, org = state.settings.directory, state.settings.org
    current, open_one, failed, registration = binding_state(state.store, repo, environment)
    identity = (current or {}).get("azure_identity")
    owner_name, repo_name = repo.split("/")
    shown = open_one.final if open_one else current  # what composed.yaml gets: the pending binding once recorded, else the current
    return page(request, "app.html", status, repo=repo, owner_name=owner_name, repo_name=repo_name, registration=registration, directory=directory,
                environment=environment, environments=ENVIRONMENTS, current=current, open_request=open_one, failed=failed,
                can_change=request.state.app_repo_admin,
                principals=directory.principals_for(org, repo) if directory else [],
                warehouses=directory.warehouses_for(identity["directory_id"]) if directory and identity else [],
                effects=effects(shown), shown_secrets=sorted((shown or {}).get("secrets") or [], key=lambda s: s["env"]),
                current_secrets=sorted((current or {}).get("secrets") or [], key=lambda s: s["env"]),
                search_text=search[0] if search else "", search_folder=search[1] if search else None,
                shown_max=SHOWN_MAX, errors=list(errors), **secrets_view(request, search))


@router.get("/apps/{owner}/{repo}")
def show(request: Request, owner: str, repo: str, environment: str = Query("test"), q: str | None = Query(None),
         folder: str | None = Query(None), principal=APP_ADMIN):
    if environment not in ENVIRONMENTS:
        return _bad_environment(request)
    if q is None and folder is None:  # no search submitted: Secret Server is not searched
        return app_page(request, environment)
    text, folder_id, problem = _parse_search("" if q is None else q, folder)
    if problem:
        return app_page(request, environment, 400, [problem])
    return app_page(request, environment, search=(text, folder_id))


def _change(request, principal, environment, **choice):
    # the path parameters are not used: request.state.app_repo is the checked, stored owner/name
    state, repo = request.app.state, request.state.app_repo
    secret_choice = "add_secret" in choice or "remove_secret" in choice
    # Azure and Databricks need the directory; secrets need only Secret Server. With neither, there is nothing to change here.
    if state.settings.directory is None and not (secret_choice and state.settings.secret_server is not None):
        return page(request, "error.html", 404, message="Not found.")
    if environment not in ENVIRONMENTS:
        return _bad_environment(request)
    if not request.state.app_repo_admin:  # the writer records a change only for the repo's admin (its recheck); say so now
        return app_page(request, environment, 403, [NOT_REPO_ADMIN])
    current, open_one, _, _ = binding_state(state.store, repo, environment)
    if open_one is not None:
        return app_page(request, environment, 409, [IN_PROGRESS])
    request_id, now = new_id("r"), state.clock()
    if secret_choice and state.settings.secret_server is None:
        return app_page(request, environment, 400, [NO_SECRET_SERVER])
    if "add_secret" in choice:
        choice.update(secret_server=state.settings.secret_server, secret_user=secret_user(request))
    try:
        final = next_binding(current=current, repo=repo, org=state.settings.org, directory=state.settings.directory,
                             person_id=principal.person.id, request_id=request_id, now=now, environment=environment, **choice)
    except BindingRefused as exc:
        return app_page(request, environment, 400, [str(exc)])
    try:
        state.store.create_binding(request_id=request_id, person_id=principal.person.id, final=final, now=now, current=current)
    except Conflict as exc:
        if exc.code != "open_request_exists":  # request_exists, unknown_person: a bug, not a race the person can retry
            raise
        return app_page(request, environment, 409, [IN_PROGRESS])
    return RedirectResponse(app_url(repo, environment), status_code=303)


@router.post("/apps/{owner}/{repo}/azure-identity")
def link_identity(request: Request, owner: str, repo: str, principal_id: str = Form(""), principal=APP_ADMIN, environment: str = FORM_ENVIRONMENT):
    return _change(request, principal, environment, principal_id=principal_id)


@router.post("/apps/{owner}/{repo}/databricks")
def link_databricks(request: Request, owner: str, repo: str, warehouse_id: str = Form(""), principal=APP_ADMIN, environment: str = FORM_ENVIRONMENT):
    return _change(request, principal, environment, warehouse_id=warehouse_id)


@router.post("/apps/{owner}/{repo}/remove")
def remove(request: Request, owner: str, repo: str, what: str = Form(""), principal=APP_ADMIN, environment: str = FORM_ENVIRONMENT):
    return _change(request, principal, environment, remove=what)


@router.post("/apps/{owner}/{repo}/secrets")
def add_secret(request: Request, owner: str, repo: str, secret_id: str = Form(""), field_slug: str = Form(""), env: str = Form(""),
               principal=APP_ADMIN, environment: str = FORM_ENVIRONMENT):
    # only the id, the field and the env name are taken from the form; next_binding asks Secret Server for the rest
    sid = int(secret_id) if DIGITS.fullmatch(secret_id) else None
    return _change(request, principal, environment, add_secret={"secret_id": sid, "field_slug": field_slug, "env": env})


@router.post("/apps/{owner}/{repo}/secrets/remove")
def remove_secret(request: Request, owner: str, repo: str, env: str = Form(""), principal=APP_ADMIN, environment: str = FORM_ENVIRONMENT):
    return _change(request, principal, environment, remove_secret=env)


@router.get("/admin/directory")
def directory_page(request: Request, principal=ADMIN):
    settings = request.app.state.settings
    return page(request, "admin_directory.html", directory=settings.directory, org=settings.org)
